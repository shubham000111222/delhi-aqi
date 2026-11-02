# Model directory

`tradebot signals --save-model models/registry.json` writes a model card here, and
`S4_ml_signal` will read it - but only if the ladder actually promoted a model over the
baseline, net of costs.

The card files are gitignored on purpose. A model trained on the synthetic demo feed has
learned the generator, not the market; committing one would let `S4` trade it after a
`git pull`, which is the exact failure this project is built to avoid. Train on recorded
real data, run the acceptance gates, and keep the resulting card outside version control.

Nothing in this directory is required for the system to run. `S4` reports
`not_promoted: no model card at models/registry.json` and returns no signals.
