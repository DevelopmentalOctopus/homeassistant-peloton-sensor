"""The Home Assistant Peloton Sensor integration."""

from __future__ import annotations

from datetime import datetime
from datetime import timedelta
import logging
import time

from dateutil import tz
from homeassistant.components.sensor import SensorDeviceClass
from homeassistant.components.sensor import SensorStateClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_PASSWORD
from homeassistant.const import CONF_USERNAME
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.helpers.update_coordinator import UpdateFailed
from pylotoncycle import PylotonCycle
from pylotoncycle.pylotoncycle import PelotonLoginException
from requests.exceptions import Timeout

from .const import DOMAIN
from .const import STARTUP_MESSAGE
from .sensor import PelotonMetric
from .sensor import PelotonStat
from .sensor import PelotonSummary


_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[str] = ["binary_sensor", "sensor"]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Home Assistant Peloton Sensor from a config entry."""

    _LOGGER.debug("Loading Peloton integration")

    if DOMAIN not in hass.data:
        # Print startup message
        _LOGGER.info(STARTUP_MESSAGE)

    # Fetch current state object
    _LOGGER.debug("Logging in and setting up session to the Peloton API")
    try:
        api = await hass.async_add_executor_job(
            PylotonCycle, entry.data[CONF_USERNAME], entry.data[CONF_PASSWORD]
        )
    except PelotonLoginException as err:
        _LOGGER.warning("Peloton username or password incorrect")
        raise ConfigEntryAuthFailed from err
    except (ConnectionError, Timeout) as err:
        raise UpdateFailed("Could not connect to Peloton.") from err

    async def async_update_data() -> bool | dict:

        try:
            workouts = await hass.async_add_executor_job(api.GetRecentWorkouts, 1)
            workout_stats_summary = workouts[0]
        except IndexError as err:
            raise UpdateFailed("User has no workouts.") from err
        except (ConnectionError, Timeout) as err:
            raise UpdateFailed("Could not connect to Peloton.") from err

        workout_stats_summary_id = workout_stats_summary["id"]

        in_progress = workout_stats_summary.get("status", None) == "IN_PROGRESS"

        stat_interval = 2 if in_progress else 300

        hass.data[DOMAIN][entry.entry_id].update_interval = timedelta(seconds=2) if in_progress else timedelta(seconds=20)

        return {
            "workout_stats_detail": (
                workout_stats_detail := await hass.async_add_executor_job(
                    api.GetWorkoutMetricsById, workout_stats_summary_id, stat_interval
                )
            ),
            "workout_stats_summary": workout_stats_summary,
            "user_profile": await hass.async_add_executor_job(api.GetMe),
            "quant_data": compile_quant_data(
                workout_stats_summary=workout_stats_summary,
                workout_stats_detail=workout_stats_detail,
            ),
        }

    # TODO setup slower query when there is no active workout, and faster query when there is an active workout.
    coordinator = DataUpdateCoordinator(
        hass,
        _LOGGER,
        name=DOMAIN,
        update_method=async_update_data,
        update_interval=timedelta(seconds=10),
    )

    # Load data for domain. If not present, initialize dict for this domain.
    hass.data.setdefault(DOMAIN, {})

    # Store coordinator
    hass.data[DOMAIN][entry.entry_id] = coordinator

    # Fetch initial data so we have data when entities subscribe
    await coordinator.async_config_entry_first_refresh()

    hass.config_entries.async_setup_platforms(entry, PLATFORMS)

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id)

    return bool(unload_ok)


def compile_quant_data(
    workout_stats_summary: dict, workout_stats_detail: dict
) -> list[PelotonStat]:
    """Compiles list of quantative data."""

    # Get Timezone
    user_timezone = (
        tz.gettz(raw_tz)
        if (raw_tz := workout_stats_summary.get("timezone"))
        else tz.gettz("UTC")
    )

    # Preprocess Summaries

    summary: dict
    summaries: dict = {}
    for summary in workout_stats_detail.get("summaries", []):
        slug = summary.get("slug")
        if slug == "calories":
            summaries[slug] = PelotonSummary(
                v if isinstance((v := summary.get("value")), int) else None,  # Convert kcal to Wh
                "kcal",
                None,
            )
        elif slug == "distance":
            summaries[slug] = PelotonSummary(
                v if isinstance((v := summary.get("value")), float) else None,
                str(summary.get("display_unit")),
                None,
            )
        elif slug == "elevation":
            summaries[slug] = PelotonSummary(
                v if isinstance((v := summary.get("value")), int) else None,
                str(summary.get("display_unit")),
                None,
            )

    # Preprocess Metrics

    metric: dict
    metrics_flattened: list = []
    for metric in workout_stats_detail.get("metrics", []):
        metrics_flattened.append(metric)
        # Tread gives speed as an alternative to the "pace" stat.
        alternatives = metric.get('alternatives', [])
        if alternatives:
            metrics_flattened.extend(alternatives)
    metrics: dict = {}

    int_stats = {
        'heart_rate': ("", None),
        'resistance': ("%", None),
        'cadence': ("rpm", None),
        'output': ("W", SensorDeviceClass.POWER),
    }
    float_stats = {'speed', 'incline'}
    for metric in metrics_flattened:
        slug = metric.get("slug")
        if slug in int_stats:
            metrics[slug] = PelotonMetric(
                v if isinstance((v := metric.get("max_value")), int) else None,
                v if isinstance((v := metric.get("average_value")), int) else None,
                v if isinstance((v := (metric.get("values", []) or [None])[-1]), int) else None,
                str(metric.get("display_unit", int_stats[slug][0])),
                int_stats[slug][1],
            )
        elif slug in float_stats:
            metrics[slug] = PelotonMetric(
                v if isinstance((v := metric.get("max_value")), float) else None,
                v if isinstance((v := metric.get("average_value")), float) else None,
                v if isinstance((v := (metric.get("values", []) or [None])[-1]), float) else None,
                str(metric.get("display_unit")),
                None,
            )

    target_metrics = workout_stats_detail.get('target_metrics_performance_data', {}).get('target_metrics', [])

    actual_elapsed = round(time.time()) - workout_stats_summary.get("start_time", 0) # TODO handle failure case.

    for target in target_metrics:
        if target['offsets']['end'] >= actual_elapsed >= target['offsets']['start']:
            for metric in target['metrics']:
                name = metric.get("name")
                if name == 'speed':
                    metrics['target_speed'] = {
                      'upper': metric['upper'],
                      'lower': metric['lower'],
                    }
                elif name == 'incline':
                    metrics['target_incline'] = {
                        'upper': metric['upper'],
                        'lower': metric['lower'],
                    }


    # Build and return list.

    def make_stat(data: PelotonMetric, name: str, stat: str, icon: str):
        if data is None:
            return PelotonStat(name, None, None, None, SensorStateClass.MEASUREMENT, icon)
        return PelotonStat(
            name,
            getattr(data, stat, None),
            data.unit,
            data.device_class,
            SensorStateClass.MEASUREMENT,
            icon,
        )

    common_stats = [
        # TODO filter stats based on device class etc.
        PelotonStat(
            "Start Time",
            datetime.fromtimestamp(workout_stats_summary["start_time"], user_timezone)
            if workout_stats_summary.get("start_time", None) is not None
            else None,
            None,
            SensorDeviceClass.TIMESTAMP,
            SensorStateClass.MEASUREMENT,
            "mdi:timer-sand",
        ),
        PelotonStat(
            "End Time",
            datetime.fromtimestamp(workout_stats_summary["end_time"], user_timezone)
            if workout_stats_summary.get("end_time", None) is not None
            else None,
            None,
            SensorDeviceClass.TIMESTAMP,
            SensorStateClass.MEASUREMENT,
            "mdi:timer-sand-complete",
        ),
        PelotonStat(
            "Duration",
            duration_sec / 60
            if (
                    (duration_sec := workout_stats_summary.get("ride", {}).get("duration"))
                    and duration_sec is not None
            )
            else None,
            "min",
            None,
            SensorStateClass.MEASUREMENT,
            "mdi:timer-outline",
        ),
        PelotonStat(
            "Leaderboard: Rank",
            workout_stats_summary.get("leaderboard_rank", 0),
            None,
            None,
            SensorStateClass.MEASUREMENT,
            "mdi:trophy-award",
        ),
        PelotonStat(
            "Leaderboard: Total Users",
            workout_stats_summary.get("total_leaderboard_users", 0),
            None,
            None,
            SensorStateClass.MEASUREMENT,
            "mdi:account-group",
        ),
        PelotonStat(
            "Power Output",
            round(total_work / 3600, 4)  # Converts joules to kWh
            if "total_work" in workout_stats_summary
               and isinstance(total_work := workout_stats_summary.get("total_work"), float)
            else None,
            "Wh",
            SensorDeviceClass.ENERGY,
            SensorStateClass.MEASUREMENT,
            None,
        ),
        PelotonStat(
            "Target Incline Upper",
            metrics.get('target_incline', {}).get('upper', None),
            "%",
            None,
            SensorStateClass.MEASUREMENT,
            "mdi:slope-uphill",
        ),
        PelotonStat(
            "Target Incline Lower",
            metrics.get('target_incline', {}).get('lower', None),
            "%",
            None,
            SensorStateClass.MEASUREMENT,
            "mdi:slope-downhill",
        ),
        PelotonStat(
            "Target Speed Upper",
            metrics.get('target_speed', {}).get('upper', None),
            getattr(metrics.get('speed', {}), "unit", None), # TODO make safer
            None,
            SensorStateClass.MEASUREMENT,
            "mdi:speedometer",
        ),
        PelotonStat(
            "Target Speed Lower",
            metrics.get('target_speed', {}).get('lower', None),
            getattr(metrics.get('speed', {}), "unit", None), # TODO make safer
            None,
            SensorStateClass.MEASUREMENT,
            "mdi:speedometer-slow",
        ),
    ]

    return common_stats + [
        # TODO filter stats based on device class etc. Let user configure only-bike or only-treadmill
        make_stat(summaries.get("distance"), "Distance", "total", "mdi:map-marker-distance"),
        make_stat(summaries.get("calories"), "Calories", "total", "mdi:fire"),
        make_stat(metrics.get("heart_rate"), "Heart Rate: Average", "avg_val", "mdi:heart-pulse"),
        make_stat(metrics.get("heart_rate"), "Heart Rate: Max", "max_val", "mdi:heart-pulse"),
        make_stat(metrics.get("resistance"), "Resistance: Average", "avg_val", "mdi:network-strength-2"),
        make_stat(metrics.get("resistance"), "Resistance: Max", "max_val", "mdi:network-strength-4"),
        make_stat(metrics.get("speed"), "Speed: Average", "avg_val", "mdi:speedometer-medium"),
        make_stat(metrics.get("speed"), "Speed: Max", "max_val", "mdi:speedometer"),
        make_stat(metrics.get("speed"), "Speed: Most Recent", "last_val", "mdi:speedometer"),
        make_stat(metrics.get("incline"), "Incline: Average", "avg_val", "mdi:slope-uphill"),
        make_stat(metrics.get("incline"), "Incline: Max", "max_val", "mdi:slope-uphill"),
        make_stat(metrics.get("incline"), "Incline: Most Recent", "last_val", "mdi:slope-uphill"),
        make_stat(metrics.get("cadence"), "Cadence: Average", "avg_val", "mdi:fan"),
        make_stat(metrics.get("cadence"), "Cadence: Max", "max_val", "mdi:fan-chevron-up"),
    ]
