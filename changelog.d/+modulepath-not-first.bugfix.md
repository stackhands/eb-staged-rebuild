`childEnv` puts the live module directory first on `MODULEPATH`, once.
A later exact copy is dropped, as is a component that names the same
directory after lexical normalization, and every other component is kept
as written, including an empty one. Lmod then searches the live tree
before any other directory.
