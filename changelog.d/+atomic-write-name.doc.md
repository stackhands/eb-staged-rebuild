The design note's atomic-write temporary file is `.{basename}.new`. A name already there is refused unless it is a regular file with one hard link, owned by the writer or the metadata owner.
