import json,os
from datetime import datetime,timedelta,timezone
from pathlib import Path
import paper_bot,trading_database
from . import broker,config,strategy
STATE=Path.home()/"ai-paper-trader-data"/"crypto_state.json"
def now():return datetime.now(timezone.utc)
def default():return {'enabled':False,'mode':config.mode(),'last_entry_at':None,'entries':[],'closed':[],'tracked':{},'last_decision':'Crypto automation stopped.','last_scan_at':None,'errors':0}
def load():
    try:d=json.loads(STATE.read_text())
    except Exception:d={}
    x=default();x.update(d if isinstance(d,dict) else {});return x
def save(s):STATE.parent.mkdir(parents=True,exist_ok=True);STATE.write_text(json.dumps(s,indent=2,sort_keys=True)+"\n")
def start():s=load();s['enabled']=True;s['last_decision']='Crypto automation enabled.';save(s);return s
def stop():s=load();s['enabled']=False;s['last_decision']='Crypto entries stopped; exits remain active.';save(s);return s
def set_mode(m):
    if m not in {'balanced','aggressive'}:raise ValueError('Mode must be balanced or aggressive.')
    import bot_config
    data=bot_config.get_config(force_reload=True);data.setdefault('crypto',{})['mode']=m
    Path('bot_config.json').write_text(json.dumps(data,indent=2)+"\n")
    s=load();s['mode']=m;save(s);return s
def entries_24h(s):
    cutoff=now()-timedelta(hours=24); out=[]
    for x in s.get('entries',[]):
        try:
            if datetime.fromisoformat(x['time'])>=cutoff:out.append(x)
        except Exception:pass
    s['entries']=out;return out
def closed_24h(s):
    cutoff=now()-timedelta(hours=24);return [x for x in s.get('closed',[]) if datetime.fromisoformat(x['time'])>=cutoff]
def cooldown(s):
    if not s.get('last_entry_at'):return False
    return now()-datetime.fromisoformat(s['last_entry_at'])<timedelta(minutes=config.cooldown_minutes())
def scan():
    assets=broker.tradable_assets(); watch=[x for x in config.get('watchlist') if x in assets]
    btc=strategy.analyze('BTC/USD',broker.recent_bars('BTC/USD','1Hour',30),True)
    healthy=bool(btc and btc['price']>btc['ema50'])
    results=[]
    for sym in watch:
        r=strategy.analyze(sym,broker.recent_bars(sym,'1Hour',30),healthy)
        if r:results.append(r)
    return sorted(results,key=lambda x:(x['score'],x['atr_pct']),reverse=True),healthy

def _record_exit(s,p,reason):
    sym=broker.normalize(p['symbol']);entry=float(p.get('avg_entry_price') or 0);current=float(p.get('current_price') or 0);qty=float(p.get('qty') or 0);pnl=(current-entry)*qty
    result=broker.submit_sell(sym,qty);s.setdefault('closed',[]).append({'time':now().isoformat(),'symbol':sym,'pnl':pnl,'reason':reason});s['closed']=s['closed'][-200:]
    trading_database.log_trade(sym,'sell',status=result.get('status'),quantity=qty,entry_price=entry,exit_price=current,pnl=pnl,reason=reason,order_id=result.get('id'),source='crypto',details=p)
    return f"CRYPTO SELL {sym}: {qty:g} because {reason}. Status {result.get('status','submitted')}"
def exits(s,positions,orders):
    events=[];pending={broker.normalize(o.get('symbol')) for o in orders if o.get('side')=='sell'}
    for p in positions:
        sym=broker.normalize(p['symbol']); current=float(p.get('current_price') or 0);entry=float(p.get('avg_entry_price') or 0)
        if sym in pending or not current or not entry:continue
        bars=broker.recent_bars(sym,'15Min',7);a=strategy.atr(bars);tracked=s.setdefault('tracked',{}).setdefault(sym,{'entry_time':now().isoformat(),'high':current});tracked['high']=max(float(tracked.get('high',0)),current)
        if not a:continue
        stop=entry-config.get('stop_atr_multiple')*a;target=entry+config.get('target_atr_multiple')*a;trail=tracked['high']-config.get('trailing_atr_multiple')*a;armed=tracked['high']>=entry+config.get('trail_arm_atr_multiple')*a
        reason=None
        if current<=stop:reason=f"ATR stop {stop:.2f}"
        elif current>=target:reason=f"ATR target {target:.2f}"
        elif armed and current<=trail:reason=f"ATR trailing stop {trail:.2f}"
        elif now()-datetime.fromisoformat(tracked['entry_time'])>=timedelta(hours=float(config.get('max_hold_hours'))):reason='maximum hold time'
        if reason:events.append(_record_exit(s,p,reason));s['tracked'].pop(sym,None)
    return events
def run_cycle():
    s=load();s['last_scan_at']=now().isoformat();events=[]
    try:
        positions=broker.crypto_positions();orders=broker.crypto_orders('open');events+=exits(s,positions,orders)
        entries=entries_24h(s);loss=sum(float(x.get('pnl') or 0) for x in closed_24h(s))
        if not s['enabled']:s['last_decision']='Crypto automation stopped.'
        elif orders:s['last_decision']='Waiting for crypto order to finish.'
        elif len(positions)>=config.max_positions():s['last_decision']='Crypto position limit reached.'
        elif len(entries)>=config.max_entries_24h():s['last_decision']='Rolling 24-hour entry limit reached.'
        elif loss<=-config.loss_limit_24h():s['last_decision']='Rolling 24-hour crypto loss limit reached.'
        elif cooldown(s):s['last_decision']='Crypto cooldown active.'
        elif events:s['last_decision']='Processed crypto exit; waiting before re-entry.'
        else:
            candidates,healthy=scan();held={broker.normalize(p['symbol']) for p in positions};eligible=[x for x in candidates if x['symbol'] not in held]
            for x in candidates:
                trading_database.log_crypto_decision(x,'PASS','scan candidate',healthy)
            best=eligible[0] if eligible else None
            if not best or best['score']<config.minimum_score():s['last_decision']=f"No crypto entry. Best: {best['symbol']+' '+str(best['score'])+'/8' if best else 'none'}"
            else:
                result=broker.submit_buy(best['symbol'],config.position_size());s['last_entry_at']=now().isoformat();s.setdefault('entries',[]).append({'time':s['last_entry_at'],'symbol':best['symbol']});s['last_decision']=f"Bought {best['symbol']} score {best['score']}/8"
                trading_database.log_crypto_decision(best,'BUY',s['last_decision'],healthy)
                trading_database.log_trade(best['symbol'],'buy',status=result.get('status'),dollars=config.position_size(),entry_price=best['price'],reason=f"crypto score {best['score']}/8",order_id=result.get('id'),source='crypto',details=best)
                events.append(f"CRYPTO BUY {best['symbol']}: ${config.position_size():.2f}, score {best['score']}/8. Status {result.get('status','submitted')}")
        s['errors']=0
    except Exception as e:
        s['errors']=int(s.get('errors',0))+1;s['last_decision']=f"Crypto error: {e}"
        if s['errors']>=6:s['enabled']=False;events.append(f"CRYPTO AUTOMATION STOPPED after repeated errors: {e}")
    save(s);return events
def panic():
    s=stop();events=[]
    for p in broker.crypto_positions():events.append(_record_exit(s,p,'manual crypto panic'))
    save(s);return events or ['Crypto automation stopped. No crypto positions were open.']
def status():
    s=load();positions=broker.crypto_positions();entries=entries_24h(s);loss=sum(float(x.get('pnl') or 0) for x in closed_24h(s))
    lines=['AI Paper Trader Crypto',f"Automation: {'RUNNING' if s['enabled'] else 'STOPPED'}",f"Mode: {config.mode().upper()}",f"Paper-only lock: {'ON' if 'paper-api.alpaca.markets' in os.environ.get('APCA_API_BASE_URL','') else 'FAILED'}",f"Positions: {len(positions)}/{config.max_positions()}",f"Entries last 24h: {len(entries)}/{config.max_entries_24h()}",f"Realized P/L last 24h: {paper_bot.money(loss)}",f"Position size: {paper_bot.money(config.position_size())}",f"Last decision: {s.get('last_decision')}"]
    for p in positions:lines.append(f"- {broker.normalize(p['symbol'])}: {paper_bot.money(p.get('market_value'))}, P/L {paper_bot.money(p.get('unrealized_pl'))}")
    return '\n'.join(lines)
