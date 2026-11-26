import sys, json, itertools, numpy as np, pandas as pd
sys.path.insert(0,'research'); sys.argv=['x','/home/claude/qdata']
import compress_lab as L, quant_bt as Q
F,fund=L.prepare()
end=max(f.index[-1] for f in F.values()); t_hold=end-pd.DateOffset(months=6); t0s=min(f.index[0] for f in F.values()); t_oos=t0s+pd.DateOffset(months=12)
# αναπαραγωγή του αρχικού grid του quant_bt (8 συνδυασμοί) με walk-forward όπως τότε (σκορ: μέσο R, >=30 trades, χωρίς όριο θέσεων)
V={}
for sq,vm,rg in itertools.product([0.1,0.2],[1.0,1.5],["btc","none"]):
    V[f"q{sq}_{vm}_{rg}"]=dict(L.BASE,sq=sq,vm=vm,reg=rg)
cache={k:L.candidate_trades(F,fund,p) for k,p in V.items()}
t=t0s+pd.DateOffset(months=12); tt=[]
while t+pd.DateOffset(months=3)<=t_hold:
    a=t-pd.DateOffset(months=12)
    def sc(k):
        x=[c for c in cache[k] if a<=c["t_in"]<t]
        if len(x)<30: return -9
        return np.mean([(c["gross"]-c["cost"]-c["funding"])/c["dist"] for c in x])
    b=max(V,key=sc); tt+=[c for c in cache[b] if t<=c["t_in"]<t+pd.DateOffset(months=3)]; t+=pd.DateOffset(months=3)
tt.sort(key=lambda c:c["t_in"])
tr,cv=L.portfolio(tt,L.BASE_RISK); print("REPLICA quant_bt WF (event sim):", json.dumps(L.stats(tr,cv)))
# συνδυασμοί (μεγαλύτερος κίνδυνος overfitting)
E={"E_vol2_trail2":dict(L.BASE,filt=["vol2"],trail=2.0),"E_calm_trail2":dict(L.BASE,reg="btc_calm",trail=2.0),
   "E_vol2_calm":dict(L.BASE,filt=["vol2"],reg="btc_calm")}
safe=dict(L.BASE_RISK,dd_cut=True)
out={"replica_wf":L.stats(tr,cv)}
for k,p in list(E.items())+[("B_vol2",dict(L.BASE,filt=["vol2"])),("C_trail2",dict(L.BASE,trail=2.0)),("A_reg_btc_calm",dict(L.BASE,reg="btc_calm")),("baseline",L.BASE)]:
    c=L.candidate_trades(F,fund,p)
    for rn,r in [("1%",L.BASE_RISK),("1%+ddcut",safe)]:
        s=L.stats(*L.portfolio(c,r,start=t_oos,end=t_hold)); d=L.stats(*L.portfolio(c,r,cost_mult=2,start=t_oos,end=t_hold)); h=L.stats(*L.portfolio(c,r,start=t_hold))
        out[f"{k}|{rn}"]={"oos":s,"dbl":d,"hold":h}
        print(f"{k:16}{rn:9}", s["trades"], "cagr",s["cagr_pct"],"PF",s["profit_factor"],"Sh",s["sharpe"],"DD",s["max_dd_pct"],"yrs",s["positive_years"],s["by_year_pct"],"| 2x cagr",d["cagr_pct"],"| hold",h["net_return_pct"],h["max_dd_pct"])
json.dump(out,open('docs/compress_lab_extra.json','w'),ensure_ascii=False,indent=1,default=str)
