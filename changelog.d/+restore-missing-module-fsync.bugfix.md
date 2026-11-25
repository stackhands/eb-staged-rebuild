A retry that finds a created module file already removed flushes its directory, so `rollback --to` and publish recovery do not record the removal before it is durable.
