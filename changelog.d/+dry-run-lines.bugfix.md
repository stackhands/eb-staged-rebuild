Off a terminal, EasyBuild 5.4's rich output wraps each dry-run line at 80 columns, so `run` without `--module` found no module line and refused. A wrapped module line is now read whole.
