"""Named policy values and their sources."""


# A longest job of 120 hours, assumed and never measured at a site, plus one
# day.  Operators must override this value once the site's longest job is
# known.
NOTES_MAX_WALLTIME_HOURS = 120
RETIREMENT_SAFETY_MARGIN_HOURS = 24
DEFAULT_GRACE_HOURS = NOTES_MAX_WALLTIME_HOURS + RETIREMENT_SAFETY_MARGIN_HOURS
SECONDS_PER_HOUR = 60 * 60

# Run identifiers use UTC and are also human-sortable in hidden retired names.
RUN_ID_TIMESTAMP_FORMAT = "%Y%m%d-%H%M%S"
