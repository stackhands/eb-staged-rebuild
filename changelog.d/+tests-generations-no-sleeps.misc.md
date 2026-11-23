The generations tests and `tests/differential.sh` wait for the paused rollback with a named pipe. They wait for the processes that hold trees in use with standard input. Neither sleeps or polls.
