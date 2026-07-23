from datetime import datetime,timedelta,timezone
import trading_database
from . import broker,config,strategy

def run(days=365):
    end=datetime.now(timezone.utc);start=end-timedelta(days=days);fmt=lambda x:x.isoformat().replace('+00:00','Z')
    fee=(float(config.get('fee_bps_per_side'))+float(config.get('slippage_bps_per_side')))/10000
    rows=[]
    for sym in config.get('watchlist'):
        bars=broker.historical_bars(sym,'1Hour',fmt(start),fmt(end)); trades=[];position=None
        for i in range(60,len(bars)-1):
            window=bars[:i+1];btc_ok=True;r=strategy.analyze(sym,window,btc_ok)
            if position:
                price=float(bars[i]['c']);a=strategy.atr(window);position['high']=max(position['high'],price);reason=None
                if a:
                    if price<=position['entry']-config.get('stop_atr_multiple')*a:reason='stop'
                    elif price>=position['entry']+config.get('target_atr_multiple')*a:reason='target'
                    elif position['high']>=position['entry']+config.get('trail_arm_atr_multiple')*a and price<=position['high']-config.get('trailing_atr_multiple')*a:reason='trail'
                if i-position['i']>=int(config.get('max_hold_hours')):reason=reason or 'time'
                if reason:
                    exitp=float(bars[i+1]['o'])*(1-fee);pnl=config.position_size()*(exitp/position['entry']-1)-config.position_size()*fee;trades.append(pnl);position=None
            elif r and r['score']>=config.minimum_score():position={'entry':float(bars[i+1]['o'])*(1+fee),'high':float(bars[i+1]['o']),'i':i+1}
        rows.append({'symbol':sym,'trades':len(trades),'pnl':sum(trades),'wins':sum(x>0 for x in trades),'losses':sum(x<0 for x in trades),'pf':sum(x for x in trades if x>0)/abs(sum(x for x in trades if x<0)) if sum(x for x in trades if x<0)<0 else 0})
    result={'days':days,'mode':config.mode(),'rows':rows,'net_pnl':sum(x['pnl'] for x in rows),'trades':sum(x['trades'] for x in rows)};result['run_id']=trading_database.save_crypto_backtest_result(result);return result
def report(r):
    lines=['AI Paper Trader Crypto Backtest',f"Run ID: {r.get('run_id')}",f"Mode: {r.get('mode','?').upper()}",f"Period: about {r.get('days')} days",f"Trades: {r.get('trades')}",f"Net P/L: ${r.get('net_pnl',0):.2f}",f"Modeled fee + slippage: {float(config.get('fee_bps_per_side'))+float(config.get('slippage_bps_per_side')):.1f} bps per side"]
    for x in r.get('rows',[]):lines.append(f"- {x['symbol']}: {x['trades']} trades, P/L ${x['pnl']:.2f}, PF {x['pf']:.2f}")
    lines.append('Historical simulation only; no live settings changed.')
    return '\n'.join(lines)
