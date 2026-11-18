Add opt-in `--usage-log` and `--eb-keep-logs` to `gc` and `retire`.

When enabled, both commands call `eb-keep-logs used --prefix ... --require-none`
before removing or retiring paths. `gc` keeps matching trees and generations,
and `retire` refuses to move a module file when usage is reported.
