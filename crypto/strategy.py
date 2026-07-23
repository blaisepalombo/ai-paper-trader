def sma(v,n): return sum(v[-n:])/n if len(v)>=n else None
def ema(v,n):
    if len(v)<n:return None
    a=2/(n+1); x=sum(v[:n])/n
    for p in v[n:]: x=p*a+x*(1-a)
    return x
def atr(bars,n=14):
    if len(bars)<n+1:return None
    r=[]
    for i in range(1,len(bars)):
        h,l,pc=float(bars[i]['h']),float(bars[i]['l']),float(bars[i-1]['c'])
        r.append(max(h-l,abs(h-pc),abs(l-pc)))
    return sum(r[-n:])/n
def rsi(v,n=14):
    if len(v)<n+1:return None
    d=[v[i]-v[i-1] for i in range(1,len(v))][-n:]
    g=sum(max(x,0) for x in d)/n; loss=sum(max(-x,0) for x in d)/n
    return 100 if loss==0 else 100-(100/(1+g/loss))
def analyze(symbol,bars,btc_healthy=True):
    if len(bars)<60:return None
    c=[float(x['c']) for x in bars]; vol=[float(x.get('v') or 0) for x in bars]
    p=c[-1]; e20=ema(c,20); e50=ema(c,50); a=atr(bars); rs=rsi(c)
    high20=max(c[-21:-1]); avgvol=sum(vol[-21:-1])/20 if len(vol)>=21 else 0
    score=0; reasons=[]
    tests=[(p>e20,'above EMA20'),(e20>e50,'EMA20 above EMA50'),(c[-1]>c[-4],'3-hour momentum'),(p>=high20,'20-hour breakout'),(avgvol and vol[-1]>=avgvol*1.15,'volume expansion'),(rs and 52<=rs<=75,'healthy RSI'),(a and a/p*100>=0.25,'enough volatility'),(symbol=='BTC/USD' or btc_healthy,'BTC regime healthy')]
    for ok,label in tests:
        if ok:score+=1;reasons.append(label)
    # pullback bonus instead of only chasing breakouts
    pullback=e20 and e50 and e20>e50 and min(c[-4:])<=e20*1.01 and p>e20
    if pullback and score<8: score+=1; reasons.append('trend pullback recovered')
    return {'symbol':symbol,'price':p,'score':min(score,8),'reasons':reasons,'atr':a,'atr_pct':a/p*100 if a else 0,'ema20':e20,'ema50':e50,'rsi':rs}
