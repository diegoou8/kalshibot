---
name: ml-tune
description: Evaluate the ML brain model status, check calibration on recent settlements, and recommend next training actions
---

When invoked, audit the ML brain:

1. Check if `data/models/` directory exists and list any `.pkl` files inside
2. Check if `src/brain/` directory exists (the BrainModel Protocol implementation)
3. If brain does NOT exist yet:
   - Report what data is available for training (query DWTrader.db: settlement count, weather_data rows, scan count)
   - Recommend building the brain: "Run /project:build-brain to create the initial logistic regression model"
   - List the features that could be used based on available data
4. If brain EXISTS:
   a. Load the model metadata from `data/models/brain_v{N}_meta.json`
   b. Query DWTrader.db for settlements since last training date
   c. Calculate Brier score and log-loss on recent settlements
   d. Compare ML predictions vs market implied probabilities (mid-price)
   e. Check calibration: are predicted 70% events winning 70% of the time?

Report format:
```
ML BRAIN STATUS — {timestamp}
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Model: brain_v1 (trained 2025-01-10, 847 training samples)
Status: LOADED ✅

Recent Performance (last 30 settled markets):
  Brier Score: 0.18 (target: < 0.20) ✅
  Log-loss:    0.41 (target: < 0.45) ✅
  Win rate (top quintile EV trades): 68%

Calibration:
  Predicted 0.8+ → actual win rate: 74% (slight underconfidence)
  Predicted 0.5-0.7 → actual win rate: 61% (well calibrated)

Recommendation: Model is performing well. Consider retraining after
50 more settlements to incorporate recent market behavior.

Next action: /project:train-brain --from 2025-01-10 (retrain with new data)
```

Always end with a concrete recommended next action.
