import numpy as np
import pandas as pd
import time
from src.layer2.pipeline import Layer2Pipeline

def run_scenario(name, num_steps, latent_func, price_gen_func, spread_gen_func, vol_func):
    pipeline = Layer2Pipeline()
    metrics = []
    
    current_time = time.time()
    ewma_alpha = 0.2
    ewma_p = None
    
    for i in range(num_steps):
        p_latent = latent_func(i)
        p_market = price_gen_func(i, p_latent)
        spread = spread_gen_func(i)
        vol = vol_func(i)
        
        if ewma_p is None:
            ewma_p = p_market
        else:
            ewma_p = ewma_alpha * p_market + (1 - ewma_alpha) * ewma_p
        
        obs = {
            'timestamp': current_time + i * 1.0, # 1 second steps
            'market_id': 'TEST_MARKET',
            'best_bid': max(0.001, p_market - spread/2),
            'best_ask': min(0.999, p_market + spread/2),
            'bid_volume': 1000,
            'ask_volume': 1000,
            'trade_volume': 100,
            'last_price': p_market,
            'news_signal': 0.0,
            'expiry_time': current_time + 3600
        }
        
        signal = pipeline.process_observation(obs)
        
        pf = pipeline.particle_filter
        weights = pf.weights['TEST_MARKET']
        ess = 1.0 / np.sum(weights**2)
        
        metrics.append({
            'step': i,
            'p_latent': p_latent,
            'p_market': p_market,
            'p_ewma': ewma_p,
            'p_true': signal.fair_probability,
            'particle_variance': pf.particles['TEST_MARKET'].var(),
            'ess': ess,
            'confidence': signal.confidence,
            'spread': spread,
            'EV_raw': signal.EV_raw
        })
        
    df = pd.DataFrame(metrics)
    
    mae_market = np.mean(np.abs(df['p_market'] - df['p_latent']))
    mae_pf = np.mean(np.abs(df['p_true'] - df['p_latent']))
    mae_ewma = np.mean(np.abs(df['p_ewma'] - df['p_latent']))
    smoothing_ratio = df['p_true'].std() / (df['p_market'].std() + 1e-9)
    avg_ess = df['ess'].mean()
    min_ess = df['ess'].min()
    
    return df, {
        'name': name,
        'mae_market': mae_market,
        'mae_ewma': mae_ewma,
        'mae_pf': mae_pf,
        'smoothing_ratio': smoothing_ratio,
        'avg_ess': avg_ess,
        'min_ess': min_ess
    }

results = []
output = "# Layer 2 Probability Engine Robustness Metrics\n\n"

# 1. Stable Market
def lat_1(i): return 0.50
def price_1(i, lat): return lat + np.random.normal(0, 0.02)
def spread_1(i): return 0.02
def vol_1(i): return 0.01
df1, res1 = run_scenario("Stable Market", 100, lat_1, price_1, spread_1, vol_1)
results.append(res1)

# 2. Permanent Jump
def lat_2(i): return 0.50 if i < 40 else 0.70
def price_2(i, lat): return lat + np.random.normal(0, 0.02)
df2, res2 = run_scenario("Permanent Jump", 100, lat_2, price_2, spread_1, vol_1)
results.append(res2)
post_jump = df2[df2['step'] >= 40]
lag_ticks = len(post_jump[np.abs(post_jump['p_true'] - 0.70) > 0.05])
res2['adaptation_lag'] = lag_ticks

# 3. Wide Spread / Low Liquidity
def price_wide(i, lat): return lat + np.random.normal(0, 0.04 if i >= 40 else 0.02)
def spread_wide(i): return 0.02 if i < 40 else 0.15
df3, res3 = run_scenario("Wide Spread", 100, lat_1, price_wide, spread_wide, vol_1)
results.append(res3)
pre_shock_unc = df3[df3['step'] < 40]['particle_variance'].mean()
post_shock_unc = df3[df3['step'] >= 40]['particle_variance'].mean()
res3['unc_increase'] = post_shock_unc / (pre_shock_unc+1e-9)

# 4. Transient False Jump
def lat_4(i): return 0.50
def price_4(i, lat):
    if 40 <= i <= 42: return 0.70 + np.random.normal(0, 0.02)
    return lat + np.random.normal(0, 0.02)
df4, res4 = run_scenario("Transient False Jump", 100, lat_4, price_4, spread_1, vol_1)
results.append(res4)
max_false_dev = df4[(df4['step'] >= 40) & (df4['step'] <= 50)]['p_true'].max() - 0.50
res4['overreaction_mag'] = max_false_dev

# 5. Slow Drifting
def lat_5(i): return 0.50 + i * 0.002
def price_5(i, lat): return lat + np.random.normal(0, 0.02)
df5, res5 = run_scenario("Slow Drifting", 100, lat_5, price_5, spread_1, vol_1)
results.append(res5)

output += "## Quantitative Backtest Results\\n\\n"
output += "| Scenario | Market Error (MAE) | EWMA Error (MAE) | Particle Filter (MAE) | Smoothing Ratio |\\n"
output += "|----------|--------------------|------------------|-----------------------|-----------------|\\n"

for r in results:
    output += f"| {r['name']} | {r['mae_market']:.5f} | {r['mae_ewma']:.5f} | {r['mae_pf']:.5f} | {r['smoothing_ratio']:.5f} |\\n"

output += "\\n## Deep-Dive Metrics\\n\\n"
output += f"- **Jump Adaptation Lag (>5% error):** {results[1].get('adaptation_lag')} ticks\\n"
output += f"- **Uncertainty Growth on Wide Spread:** {results[2].get('unc_increase'):.2f}x\\n"
output += f"- **Transient Jump Overreaction Magnitude:** {results[3].get('overreaction_mag'):.4f} (Max target jump was 0.20)\\n"

with open("layer2_robustness_metrics.md", "w") as f:
    f.write(output)
