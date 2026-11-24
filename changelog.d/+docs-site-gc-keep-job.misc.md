The `gc-keep` case of `docs/examples/site.sh` waits for its job to say it is in the replaced tree and ends it with a line on its input, instead of polling `/proc` and killing a `sleep 600`.
