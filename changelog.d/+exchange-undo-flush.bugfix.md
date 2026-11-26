A parent flush that fails after a successful `RENAME_EXCHANGE` now
flushes that parent again once the directories are exchanged back.
A crash after the command reports failure no longer leaves the
exchanged names. A failed undo reports that failure.
