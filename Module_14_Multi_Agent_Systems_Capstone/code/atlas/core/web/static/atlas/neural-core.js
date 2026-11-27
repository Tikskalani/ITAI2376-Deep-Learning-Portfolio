// The Today room: Neural Core (approved design 2026-10-22). The drawing code
// below is the reference's, unchanged: the network, synapses, packets,
// ripples, glow sprites and quotes. What changed is what feeds it.
//
// Live: GET /api/core/state for the ring, decisions, health and the recent
// feed; /api/core/events (SSE) for hand-offs, agent states and decision
// changes. No events, no packets: the network only idles. Every summary
// arrives already templated and privacy-gated by core/core_view.py.
// Demo (/?demo=1, window.ATLAS_DEMO): the reference's script and fake
// approvals; it never calls an approval endpoint.
(function () {
  const DEMO = !!window.ATLAS_DEMO;
  const PACKET_GAP_MS = 400, BACKLOG_PACKETS = 6, REFRESH_MS = 60000;
  // The brief's rule: over 6 ms a frame at 1440x900 on this laptop, use the 76-node network.
  // Deployed, live (12 agents) measured 11.2 ms with the approved 132 nodes and 6.0 ms with
  // 76 (GPU, 2026-10-22), so 76 it is. ATLAS_CORE_LAYERS overrides it for the bench.
  const LAYERS = window.ATLAS_CORE_LAYERS || [6, 10, 14, 16, 14, 10, 6];

  function whenReady(fn) {
    if (document.getElementById("nc-cv")) return fn();
    const mo = new MutationObserver(() => { if (document.getElementById("nc-cv")) { mo.disconnect(); fn(); } });
    mo.observe(document.documentElement, { childList: true, subtree: true });
  }

  function start() {
  const root = document.getElementById("nc");
  const $ = (id) => document.getElementById(id);
  var cv = $("nc-cv"), ctx = cv.getContext("2d");
  var reduce = window.matchMedia && matchMedia("(prefers-reduced-motion: reduce)").matches;
  var W, H, DPR, cx, cy, rx, ry, ns, narrow;
  function rng(a){return function(){a|=0;a=a+0x6D2B79F5|0;var t=Math.imul(a^a>>>15,1|a);t=t+Math.imul(t^t>>>7,61|t)^t;return((t^t>>>14)>>>0)/4294967296;};}
  var R = rng(7);

  /* ---------- agents ---------- */
  // Demo: the reference's eight. Live: the ring from config/core_view.yaml.
  var DEMO_LASTS={'COO':'Drafted the reply to Dana at Bayou Lending. Waiting on your yes.','CTO':'Fixed a flaky planner test at 01:48.','CAREER':'Tailored the resume for two roles at 23:42.','CLIENT DELIVERY':'Drafted the Helix Data update at 00:24.','ACADEMIC':'Built a study plan for the week at 01:08.','RESEARCH':'Summarized the Helix Data thread, 4 open questions.','FINANCE':'Spend is $0.84 of $8.00 today. Night budget untouched.','OPS':'Started the nightly suite.'};
  var DEMO_AG=[['COO','WAITING ON YOU','w'],['CTO','ACTIVE','a'],['CAREER','IDLE',''],['CLIENT DELIVERY','DRAFT READY',''],['ACADEMIC','IDLE',''],['RESEARCH','IDLE',''],['FINANCE','WATCHING SPEND',''],['OPS','IDLE','']];
  var AG = [];
  function makeAgents(list){ /* list: [{id,name,state,last}] */
    $("nc-agents").replaceChildren();
    AG = list.map(function(a,i){return {id:a.id,name:a.name,state:a.state,base:a.state,flag:'',i:i,x:0,y:0,flash:0,hold:0,last:a.last};});
    AG.forEach(function(a,i){var b=document.createElement('button');b.className='nc-ab';b.setAttribute('aria-label',a.name+' agent. Show what it is doing; open its record.');
      /* a node opens that agent's record in Staff: purpose, runs, spend, traces (finding 09) */
      b.addEventListener('click',function(){if(!DEMO)location.hash='#agent/'+encodeURIComponent(a.id);});
      b.addEventListener('mouseenter',function(){hov=i;showTip(i);});b.addEventListener('focus',function(){hov=i;showTip(i);});
      b.addEventListener('mouseleave',function(){hov=-1;hideTip();});b.addEventListener('blur',function(){hov=-1;hideTip();});
      $("nc-agents").appendChild(b);a.el=b;});
    layout();wire();
  }
  function agentIdx(n){for(var i=0;i<AG.length;i++)if(AG[i].name===n)return i;return -1;}

  /* ---------- neural net ---------- */
  var LAY=LAYERS,nodes=[],edges=[],outs=[];
  (function(){
    for(var k=0;k<LAY.length;k++){
      var n=LAY[k],x=(k/(LAY.length-1)-.5)*2*250,Rk=40+125*Math.sin(Math.PI*(k+.5)/LAY.length);
      for(var j=0;j<n;j++){
        var a=j/n*Math.PI*2+k*.55,rr=Rk*(.5+.5*Math.sqrt(R()));
        nodes.push({k:k,x:x+(R()-.5)*26,y:rr*Math.cos(a),z:rr*Math.sin(a),act:0,sx:0,sy:0,sc:1,zz:0});
      }
    }
    for(var a=0;a<nodes.length;a++){
      outs[a]=[];var A=nodes[a];if(A.k===LAY.length-1)continue;
      var c=[];for(var b=0;b<nodes.length;b++)if(nodes[b].k===A.k+1){var dx=nodes[b].x-A.x,dy=nodes[b].y-A.y,dz=nodes[b].z-A.z;c.push([b,dx*dx*.1+dy*dy+dz*dz]);}
      c.sort(function(p,q){return p[1]-q[1];});
      var pick=[c[0][0],c[1][0],c[2][0],c[Math.floor(R()*c.length)][0]];
      pick.filter(function(v,i){return pick.indexOf(v)===i;}).forEach(function(b){outs[a].push(edges.length);edges.push({a:a,b:b,glow:0});});
    }
  })();
  var SH=[];for(var q0=0;q0<260;q0++){var u=R()*2-1,ph=R()*6.2832,rad=270+R()*170,ss=Math.sqrt(1-u*u);SH.push({x:rad*ss*Math.cos(ph)*1.25,y:rad*u*.8,z:rad*ss*Math.sin(ph),a:.15+R()*.4,s:.5+R()*1.1});}
  var byLayer=[];nodes.forEach(function(n,i){(byLayer[n.k]=byLayer[n.k]||[]).push(i);});
  var fpulses=[];
  function wire(){AG.forEach(function(a){
    /* a window measured at zero (a hidden pane) gives no position yet: wire to the middle */
    var f=Math.max(0,Math.min(1,(a.x-cx)/(rx*2)+.5));if(!isFinite(f))f=.5;var k=Math.round(f*(LAY.length-1));
    if(Math.abs(Math.sin(a.ang))>.85)k=Math.max(2,Math.min(LAY.length-3,k));
    var L=byLayer[k],R2=rng(31+a.i*7);a.syn=[];
    while(a.syn.length<3){var c=L[Math.floor(R2()*L.length)];if(a.syn.indexOf(c)<0)a.syn.push(c);}});}
  var first=[];nodes.forEach(function(n,i){if(n.k===0)first.push(i);});

  /* ---------- sprites ---------- */
  function sprite(rgb){var c=document.createElement('canvas');c.width=c.height=64;var g=c.getContext('2d'),r=g.createRadialGradient(32,32,0,32,32,32);
    r.addColorStop(0,'rgba('+rgb+',1)');r.addColorStop(.18,'rgba('+rgb+',.55)');r.addColorStop(.5,'rgba('+rgb+',.12)');r.addColorStop(1,'rgba('+rgb+',0)');g.fillStyle=r;g.fillRect(0,0,64,64);return c;}
  var SP={net:sprite('127,178,255'),hot:sprite('216,240,255'),amber:sprite('255,176,32'),mint:sprite('92,242,176')};
  function glow(s,x,y,r,a){ctx.globalAlpha=a;ctx.drawImage(s,x-r,y-r,r*2,r*2);}

  /* ---------- layout ---------- */
  function layout(){
    DPR=Math.min(window.devicePixelRatio||1,2);W=innerWidth;H=innerHeight;
    cv.width=Math.floor(W*DPR);cv.height=Math.floor(H*DPR);ctx.setTransform(DPR,0,0,DPR,0,0);
    narrow=W<900;
    if(narrow){cx=W/2;cy=H*.25;rx=Math.min(W*.36,300);ry=H*.16;ns=Math.min(W/1000,.6);}
    else{var aw=W-420;cx=aw/2+16;cy=H*.55;rx=Math.min(aw/2-120,560);ry=Math.min(H*.29,290);ns=Math.min(aw/760,H/620,1.5);}
    /* the reference's -90+22.5+i*45 for eight, spaced the same way for any count */
    var step=360/Math.max(AG.length,1);
    AG.forEach(function(a,i){var ang=(-90+step/2+i*step)*Math.PI/180;a.ang=ang;a.x=cx+rx*Math.cos(ang);a.y=cy+ry*Math.sin(ang);if(a.el){a.el.style.left=a.x+'px';a.el.style.top=a.y+'px';}});
  }
  var hov=-1,tip=$('nc-tip');
  function showTip(i){var a=AG[i];$('nc-tn').textContent=a.name;$('nc-ts').textContent=a.state;$('nc-tl').textContent=a.last;
    var x=a.x+(a.x>cx?-286:30),y=a.y-40;x=Math.max(10,Math.min(W-280,x));tip.style.left=x+'px';tip.style.top=Math.max(10,y)+'px';tip.classList.add('on');}
  function hideTip(){tip.classList.remove('on');}
  addEventListener('resize',function(){layout();wire();});

  /* ---------- time: Houston, as the briefs use ---------- */
  var TZ='America/Chicago';
  function hhmmss(d){try{return new Intl.DateTimeFormat('en-GB',{timeZone:TZ,hour:'2-digit',minute:'2-digit',second:'2-digit',hour12:false}).format(d);}catch(e){return d.toTimeString().slice(0,8);}}
  function hourNow(){try{return +new Intl.DateTimeFormat('en-GB',{timeZone:TZ,hour:'2-digit',hour12:false}).format(new Date());}catch(e){return new Date().getHours();}}
  function greet(){var h=hourNow(),g=h<5?'GOOD EVENING':(h<12?'GOOD MORNING':(h<17?'GOOD AFTERNOON':'GOOD EVENING'));$('nc-greet').textContent=g+', TRILOK';}

  /* ---------- state ---------- */
  var pulses=[],packets=[],ripples=[],tNow=0,spawnAt=0,coreFlash=0,paused=false;
  function proj(T){
    var th=.42+Math.sin(T*.11)*.55,ct=Math.cos(th),st=Math.sin(th),ph=.30+Math.sin(T*.07)*.06,cp=Math.cos(ph),sp=Math.sin(ph);
    nodes.forEach(function(n){
      var x=n.x*ct+n.z*st,z=-n.x*st+n.z*ct,y=n.y*cp-z*sp,z2=n.y*sp+z*cp;
      var s=720/(720+z2);n.sx=cx+x*s*ns;n.sy=cy+y*s*ns;n.sc=s;n.zz=z2;});
    var th2=-T*.07+1.1,c2=Math.cos(th2),s2=Math.sin(th2);
    SH.forEach(function(n){var x=n.x*c2+n.z*s2,z=-n.x*s2+n.z*c2,y=n.y*cp-z*sp,z2=n.y*sp+z*cp,s=720/(720+z2);n.sx=cx+x*s*ns;n.sy=cy+y*s*ns;n.sc=s;n.zz=z2;});
  }
  function fire(i,delay){pulses.push({n:i,t:-(delay||0),e:-1});}
  function spawn(a){ /* pulse along each outgoing edge */
    outs[a].forEach(function(e){if(R()<.8)pulses.push({e:e,t:0,spd:.9+R()*.8});});}
  function burst(n){coreFlash=Math.min(1,coreFlash+n/30);for(var i=0;i<n;i++){var s=first[Math.floor(R()*first.length)];pulses.push({e:-2,n:s,t:-(i*.07),spd:0});}}

  /* ---------- coordination: the demo script ---------- */
  var SCRIPT=[
    ['COO','CTO','Need a send path for the Bayou reply. Is the draft_email approval row ready?',''],
    ['CTO','COO','Approval row created. Google scopes are read-only, send goes through your yes.',''],
    ['FINANCE','COO','Spend is $0.84 of $8.00. Night budget untouched.',''],
    ['CAREER','RESEARCH','Pull current role requirements for the Northwind AI engineer posting.',''],
    ['RESEARCH','CAREER','12 requirements extracted, 9 match your evidence file.',''],
    ['CLIENT DELIVERY','COO','Helix Data update drafted. Needs your review before it goes out.',''],
    ['ACADEMIC','CTO','Lab 05 test harness needs its fixture refreshed.',''],
    ['CTO','OPS','Planner flake fixed. Re-run the nightly suite.',''],
    ['OPS','CTO','Suite green. 1103 passed.',''],
    ['COO','FINANCE','Check the cap before tonight\'s three tasks.','']
  ];
  var si=0,nextMsg=1.2,clock0=12*3600+33*60+5,feed=$('nc-feed');
  function clk(T){var s=Math.floor(clock0+T),h=Math.floor(s/3600)%24,m=Math.floor(s/60)%60,x=s%60;return(h<10?'0':'')+h+':'+(m<10?'0':'')+m+':'+(x<10?'0':'')+x;}
  /* the feed shows the event's own time in live mode, the script clock in demo */
  function addFeed(from,to,text,kind,T,stamp,href){
    var empty=feed.querySelector('li.empty');if(empty)empty.remove();
    var li=document.createElement('li');if(kind)li.className=kind;
    var tm=document.createElement('time');tm.textContent=stamp||clk(T);
    var b=document.createElement('b');b.appendChild(document.createTextNode(from));var ar=document.createElement('i');ar.textContent='→';b.appendChild(ar);b.appendChild(document.createTextNode(to));
    var p=document.createElement('p');p.textContent=text;
    li.appendChild(tm);li.appendChild(b);li.appendChild(p);
    /* a message from a Chat conversation opens that conversation */
    if(href){li.classList.add('link');li.tabIndex=0;li.setAttribute('role','link');li.title='Open this conversation';
      li.addEventListener('click',function(){location.hash=href;});li.addEventListener('keydown',function(e){if(e.key==='Enter')location.hash=href;});}
    feed.insertBefore(li,feed.firstChild);
    while(feed.children.length>7)feed.removeChild(feed.lastChild);
  }
  /* Trilok (YOU) and Atlas's own loops (ATLAS) are not on the ring: their
     packets start or land off it, as the reference's send to Dana does. */
  function offPoint(name){
    if(name==='YOU')return narrow?{x:W-30,y:Math.max(30,cy-ry*1.4)}:{x:W-404,y:Math.max(60,cy-ry*1.1)};
    return {x:cx,y:Math.max(30,cy-ry*1.55)};
  }
  function send(fromN,toN,text,kind,T,toPoint,stamp,href){
    var a=agentIdx(fromN),b=toPoint?null:agentIdx(toN),ap=null;
    if(a<0){ap=offPoint(fromN);}
    if(b!==null&&b<0){toPoint=offPoint(toN);b=null;}
    packets.push({a:a,ap:ap,b:b,bp:toPoint||null,t:0,dur:1.7,label:fromN+' → '+toN,kind:kind||'',life:0});
    if(a>=0){AG[a].flash=1;AG[a].last=text;AG[a].syn.forEach(function(n,j){fpulses.push({ag:a,n:n,t:-j*.06,dir:1});});}
    burst(6);addFeed(fromN,toN,text,kind,T,stamp,href);if(a>=0&&hov===a)showTip(a);
  }

  /* ---------- hold to authorize ---------- */
  var auth=$('nc-auth'),ring=$('nc-ring'),holdStart=0,holding=false,authorized=false;
  /* decisions first; with none, the other things that need a look; "All clear." only when nothing does */
  function setHeadline(n,looks){
    var h=$('nc-h1');h.textContent='';looks=looks||0;
    if(n===0&&looks===0){var s=document.createElement('span');s.className='clear';s.textContent='All clear.';h.appendChild(s);}
    else if(n===0){var e2=document.createElement('em');e2.textContent=looks+(looks===1?' thing':' things');h.appendChild(e2);h.appendChild(document.createTextNode(looks===1?' needs a look.':' need a look.'));}
    else{var em=document.createElement('em');em.textContent=n+(n===1?' decision':' decisions');h.appendChild(em);h.appendChild(document.createTextNode(n===1?' needs you.':' need you.'));}
  }
  function setTag(text){$('nc-tagt').textContent=text;}
  /* demo: the reference's own flow, including its Undo (which resets the demo) */
  function demoAuth(on){
    authorized=on;root.classList.toggle('authd',on);
    setHeadline(on?1:2);setTag(on?'AUTHORIZED BY YOU':'NEEDS YOUR YES');AG[0].base=on?'IDLE':'WAITING ON YOU';
    AG[0].state=on?'SENDING':AG[0].base;
    if(on){var a=AG[0],ox=cx+(a.x-cx)*1.5,oy=cy+(a.y-cy)*1.5;
      send('COO','Dana · Bayou Lending','Authorized by you. Sending the reply with the signed documents attached.','ok',tNow,{x:Math.max(30,Math.min(W-30,ox)),y:Math.max(30,Math.min(H-30,oy))});
      burst(30);
      setTimeout(function(){AG[0].state=AG[0].base==='WAITING ON YOU'?'IDLE':AG[0].base;},3500);}
    else AG[0].state='WAITING ON YOU';
    ring.style.setProperty('--p',0);
  }
  /* the ring fills in the frame loop; a timer finishes the hold, so it works even when frames are throttled */
  var holdTimer=0;
  function hs(e){if(authorized||holding||!canHold())return;holding=true;holdStart=performance.now();if(e&&e.preventDefault)e.preventDefault();
    clearTimeout(holdTimer);holdTimer=setTimeout(function(){if(holding&&!authorized){holding=false;ring.style.setProperty('--p',1);authorize();}},900);}
  function he(){holding=false;clearTimeout(holdTimer);if(!authorized)ring.style.setProperty('--p',0);}
  function authorize(){ if(DEMO) demoAuth(true); else live.approve(); }
  auth.addEventListener('pointerdown',hs);addEventListener('pointerup',he);addEventListener('pointercancel',he);
  auth.addEventListener('keydown',function(e){if(e.key==='Enter'){e.preventDefault();if(!authorized&&canHold())authorize();}});
  $('nc-undo').addEventListener('click',function(){if(DEMO)demoAuth(false);});
  $('nc-decline').addEventListener('click',function(){if(!DEMO)live.decline();});
  /* the ask bar is a real input: Enter sends it to the CEO and opens Chat on the answer */
  $('nc-askin').addEventListener('keydown',function(e){
    if(e.key!=='Enter'||e.shiftKey)return;var text=this.value.trim();if(!text)return;e.preventDefault();
    if(DEMO){this.value='';location.hash='#chat/channel';return;}
    try{var app=window.Alpine.$data(document.body);app.draft=text;this.value='';app.goRoom('comms','channel');app.send();}
    catch(err){location.hash='#chat/channel';}
  });

  /* ---------- drawing ---------- */
  function bez(p,t){
    var A=p.ap?p.ap:{x:AG[p.a].x,y:AG[p.a].y},B=p.bp?p.bp:{x:AG[p.b].x,y:AG[p.b].y};
    var mx=(A.x+B.x)/2,my=(A.y+B.y)/2,k=(p.bp||p.ap)?.1:.55,C={x:mx+(cx-mx)*k,y:my+(cy-my)*k},u=1-t;
    return{x:u*u*A.x+2*u*t*C.x+t*t*B.x,y:u*u*A.y+2*u*t*C.y+t*t*B.y};}

  function draw(T,dt){
    tNow=T;
    ctx.globalCompositeOperation='source-over';ctx.globalAlpha=1;
    ctx.fillStyle='#04060A';ctx.fillRect(0,0,W,H);
    var bg=ctx.createRadialGradient(cx,cy,0,cx,cy,Math.max(rx,ry)*1.7);bg.addColorStop(0,'rgba(24,44,84,.55)');bg.addColorStop(.5,'rgba(10,18,36,.35)');bg.addColorStop(1,'rgba(4,6,10,0)');
    ctx.fillStyle=bg;ctx.fillRect(0,0,W,H);

    /* ambient firing */
    if(T>spawnAt){spawnAt=T+.28+R()*.3;fire(first[Math.floor(R()*first.length)]);}
    /* script: demo only */
    if(DEMO&&T>nextMsg&&!(reduce)){var m=SCRIPT[si%SCRIPT.length];si++;send(m[0],m[1],m[2],m[3],T);nextMsg=T+2.6+R()*1.4;}

    proj(T);
    nodes.forEach(function(n){n.act=Math.max(0,n.act-dt*1.4);});
    edges.forEach(function(e){e.glow=Math.max(0,e.glow-dt*1.6);});
    AG.forEach(function(a){a.flash=Math.max(0,a.flash-dt*.9);});

    /* pulses */
    for(var i=pulses.length-1;i>=0;i--){
      var p=pulses[i];p.t+=dt*(p.spd||2);
      if(p.e===-2||p.e===-1){ /* node ignition */
        if(p.t>=0){nodes[p.n].act=1;spawn(p.n);pulses.splice(i,1);}continue;}
      if(p.t>=1){nodes[edges[p.e].b].act=Math.min(1,nodes[edges[p.e].b].act+.9);edges[p.e].glow=1;
        if(R()<.72)spawn(edges[p.e].b);pulses.splice(i,1);}
    }
    if(pulses.length>420)pulses.splice(0,pulses.length-420);

    ctx.globalCompositeOperation='lighter';
    coreFlash=Math.max(0,coreFlash-dt*.7);
    glow(SP.net,cx,cy,(230+30*Math.sin(T*1.3))*ns,.10+.04*Math.sin(T*1.3)+coreFlash*.22);
    SH.forEach(function(n){var d=Math.max(0,Math.min(1,(n.zz+300)/600));ctx.globalAlpha=n.a*(1-d*.7);ctx.fillStyle='#9CC2FF';ctx.beginPath();ctx.arc(n.sx,n.sy,n.s*n.sc,0,6.2832);ctx.fill();});
    ctx.globalAlpha=1;
    /* synapses: each agent is wired into its layer of the network */
    function synPt(a,n,t){var mx=(a.x+n.sx)/2,my=(a.y+n.sy)/2,qx=mx+(cx-mx)*.18,qy=my+(cy-my)*.18,u=1-t;
      return{x:u*u*a.x+2*u*t*qx+t*t*n.sx,y:u*u*a.y+2*u*t*qy+t*t*n.sy,qx:qx,qy:qy};}
    AG.forEach(function(a){a.syn.forEach(function(ni){var n=nodes[ni],q=synPt(a,n,.5);
      var al=.07+a.flash*.4+(hov===a.i?.35:0);
      ctx.strokeStyle='rgba(127,178,255,'+al+')';ctx.lineWidth=hov===a.i?1.4:.9;
      ctx.beginPath();ctx.moveTo(a.x,a.y);ctx.quadraticCurveTo(q.qx,q.qy,n.sx,n.sy);ctx.stroke();});});
    for(var fp=fpulses.length-1;fp>=0;fp--){var F=fpulses[fp];F.t+=dt*2.4;if(F.t<0)continue;
      if(F.t>=1){if(F.dir>0)fire(F.n);else nodes[F.n].act=1;fpulses.splice(fp,1);continue;}
      var a2=AG[F.ag],tt2=F.dir>0?F.t:1-F.t,pp=synPt(a2,nodes[F.n],tt2);glow(SP.hot,pp.x,pp.y,10,.95);}

    /* faint layer rings so the eight layers read as a network, not a cloud */
    ctx.lineWidth=1;
    for(var lk=0;lk<LAY.length;lk++){
      var xk=(lk/(LAY.length-1)-.5)*2*250,Rk=40+125*Math.sin(Math.PI*(lk+.5)/LAY.length)*1.02;
      var th3=.42+Math.sin(T*.11)*.55,c3=Math.cos(th3),s3=Math.sin(th3),ph3=.30+Math.sin(T*.07)*.06,cp3=Math.cos(ph3),sp3=Math.sin(ph3);
      ctx.strokeStyle='rgba(127,178,255,.07)';ctx.beginPath();
      for(var sg=0;sg<=40;sg++){var an=sg/40*6.2832,yy=Rk*Math.cos(an),zz=Rk*Math.sin(an);
        var x=xk*c3+zz*s3,z=-xk*s3+zz*c3,y=yy*cp3-z*sp3,z2=yy*sp3+z*cp3,sc=720/(720+z2),px=cx+x*sc*ns,py=cy+y*sc*ns;
        sg?ctx.lineTo(px,py):ctx.moveTo(px,py);}
      ctx.stroke();}

    /* edges */
    ctx.lineWidth=1;
    edges.forEach(function(e){
      var A=nodes[e.a],B=nodes[e.b],d=Math.max(0,Math.min(1,(A.zz+B.zz)/2/260+.5));
      var al=.022+.06*(1-d)+e.glow*.6;
      ctx.strokeStyle='rgba(120,170,255,'+al+')';ctx.beginPath();ctx.moveTo(A.sx,A.sy);ctx.lineTo(B.sx,B.sy);ctx.stroke();
    });
    /* pulse heads */
    pulses.forEach(function(p){
      if(p.e<0||p.t<0)return;var e=edges[p.e],A=nodes[e.a],B=nodes[e.b],t=p.t,x=A.sx+(B.sx-A.sx)*t,y=A.sy+(B.sy-A.sy)*t;
      ctx.strokeStyle='rgba(216,240,255,.55)';ctx.lineWidth=1.2;ctx.beginPath();ctx.moveTo(A.sx+(B.sx-A.sx)*Math.max(0,t-.22),A.sy+(B.sy-A.sy)*Math.max(0,t-.22));ctx.lineTo(x,y);ctx.stroke();
      glow(SP.hot,x,y,7*ns+2,.85);
    });
    /* nodes: focus plane at the centre, far and near nodes bloom softer */
    nodes.slice().sort(function(a,b){return b.zz-a.zz;}).forEach(function(n){
      var blur=Math.min(1,Math.abs(n.zz)/170),k=n.sc*Math.max(.7,ns),r=(1.4+n.act*2.6)*k*(1+blur*.35);
      glow(SP.net,n.sx,n.sy,(6+blur*10+n.act*22)*k,(.05+n.act*.85)*(1-blur*.4));
      if(n.act>.05)glow(SP.hot,n.sx,n.sy,(5+n.act*9)*k,n.act*.9);
      ctx.globalAlpha=(.5+.48*n.act)*(1-blur*.5);ctx.fillStyle=n.act>.2?'#EAF6FF':'#9EC3FF';ctx.beginPath();ctx.arc(n.sx,n.sy,r,0,6.2832);ctx.fill();
    });

    /* packets between agents */
    for(var j=packets.length-1;j>=0;j--){
      var pk=packets[j];pk.t+=dt/pk.dur;
      var col=pk.kind==='ok'?SP.mint:SP.hot,rgb=pk.kind==='ok'?'92,242,176':'200,230,255';
      var tt=Math.min(1,pk.t),pt=bez(pk,tt);
      /* full path, faint */
      ctx.globalAlpha=1;ctx.strokeStyle='rgba('+rgb+','+(.10*(1-Math.max(0,pk.t-.7)/.5))+')';ctx.lineWidth=1;ctx.beginPath();
      for(var s=0;s<=24;s++){var q=bez(pk,s/24);s?ctx.lineTo(q.x,q.y):ctx.moveTo(q.x,q.y);}ctx.stroke();
      /* trail */
      ctx.strokeStyle='rgba('+rgb+',.85)';ctx.lineWidth=1.8;ctx.beginPath();
      var t0=Math.max(0,tt-.28);for(var s2=0;s2<=14;s2++){var q2=bez(pk,t0+(tt-t0)*s2/14);s2?ctx.lineTo(q2.x,q2.y):ctx.moveTo(q2.x,q2.y);}ctx.stroke();
      glow(col,pt.x,pt.y,22,1);glow(SP.hot,pt.x,pt.y,8,.9);
      /* chip */
      if(pk.t<1.15){var al=pk.t<.12?pk.t/.12:(pk.t>.95?Math.max(0,(1.15-pk.t)/.2):1);
        ctx.globalCompositeOperation='source-over';ctx.globalAlpha=al*.95;
        ctx.font='400 10px "Martian Mono",monospace';var w=ctx.measureText(pk.label).width+14,cxp=Math.max(w/2+8,Math.min(W-w/2-8,pt.x)),cyp=pt.y-20;
        ctx.fillStyle='rgba(6,10,18,.88)';ctx.strokeStyle='rgba('+rgb+',.5)';ctx.lineWidth=1;
        rr(cxp-w/2,cyp-10,w,18,5);ctx.fill();ctx.stroke();
        ctx.fillStyle=pk.kind==='ok'?'#5CF2B0':'#D8F0FF';ctx.textAlign='center';ctx.textBaseline='middle';ctx.fillText(pk.label,cxp,cyp-1);
        ctx.globalCompositeOperation='lighter';}
      if(pk.t>=1&&!pk.done){pk.done=1;if(pk.b!==null&&pk.b>=0){ripples.push({x:AG[pk.b].x,y:AG[pk.b].y,t:0,c:pk.kind});AG[pk.b].syn.forEach(function(n,j){fpulses.push({ag:pk.b,n:n,t:-j*.05,dir:-1});});AG[pk.b].flash=1;if(pk.b!==0||authorized||!DEMO)AG[pk.b].state='RECEIVING';(function(i){setTimeout(function(){if(AG[i]&&AG[i].state==='RECEIVING')AG[i].state=AG[i].base;},1800);})(pk.b);burst(8);}}
      if(pk.t>1.3)packets.splice(j,1);
    }

    for(var rp=ripples.length-1;rp>=0;rp--){var r0=ripples[rp];r0.t+=dt/1.1;if(r0.t>=1){ripples.splice(rp,1);continue;}
      ctx.strokeStyle=(r0.c==='ok'?'rgba(92,242,176,':'rgba(160,210,255,')+(.55*(1-r0.t))+')';ctx.lineWidth=1.4;ctx.beginPath();ctx.arc(r0.x,r0.y,12+r0.t*46,0,6.2832);ctx.stroke();}
    /* agents */
    ctx.globalCompositeOperation='source-over';
    AG.forEach(function(a,i){
      var waiting=a.state==='WAITING ON YOU',sendg=a.state==='SENDING';ctx.globalAlpha=(hov>=0&&hov!==i)?.45:1;
      var col=waiting?'#FFB020':(sendg?'#5CF2B0':'#D8F0FF'),sp=waiting?SP.amber:(sendg?SP.mint:SP.hot);
      ctx.globalCompositeOperation='lighter';
      glow(sp,a.x,a.y,26+a.flash*26,.25+a.flash*.75+(waiting?.25*(.5+.5*Math.sin(T*3)):0));
      ctx.globalCompositeOperation='source-over';ctx.globalAlpha=1;
      ctx.strokeStyle=col;ctx.lineWidth=1.2;ctx.beginPath();ctx.arc(a.x,a.y,11,0,6.2832);ctx.stroke();
      if(waiting){ctx.globalAlpha=.4+.4*Math.sin(T*3);ctx.beginPath();ctx.arc(a.x,a.y,16,0,6.2832);ctx.stroke();ctx.globalAlpha=1;}
      ctx.fillStyle=col;ctx.beginPath();ctx.arc(a.x,a.y,3.6,0,6.2832);ctx.fill();
      var ca=Math.cos(a.ang),sa=Math.sin(a.ang),lx=a.x+ca*24,ly=a.y+(sa<-.6?-36:(sa>.6?34:sa*14));
      if(narrow){lx=a.x;ly=sa>.6?a.y+30:a.y-26;}ctx.textAlign=narrow?'center':(ca>.3?'left':(ca<-.3?'right':'center'));ctx.textBaseline='middle';
      var nm=narrow&&a.name==='CLIENT DELIVERY'?'CLIENT':a.name,showSt=!narrow||waiting||sendg||a.state==='RECEIVING';ctx.fillStyle='#EAF1FF';ctx.font=(narrow?'500 12px':'500 13px')+' "Hanken Grotesk",system-ui,sans-serif';ctx.fillText(nm,lx,showSt?ly-7:ly);
      ctx.fillStyle=waiting?'#FFB020':(sendg?'#5CF2B0':'#8A98B4');ctx.font='300 9.5px "Martian Mono",monospace';if(showSt)ctx.fillText(a.state,lx,ly+8);
    });
    ctx.globalAlpha=1;
  }
  function rr(x,y,w,h,r){ctx.beginPath();ctx.moveTo(x+r,y);ctx.arcTo(x+w,y,x+w,y+h,r);ctx.arcTo(x+w,y+h,x,y+h,r);ctx.arcTo(x,y+h,x,y,r);ctx.arcTo(x,y,x+w,y,r);ctx.closePath();}

  /* ---------- live data ---------- */
  var STATE_LABEL={active:'ACTIVE',idle:'IDLE',waiting:'WAITING ON YOU',sending:'SENDING',receiving:'RECEIVING',error:'ERROR'};
  var live = {
    decisions: [], roster: [], current: null, pending: null, queue: [], lastPacket: 0, es: null, refreshT: 0,
    nameOf: function(id){for(var i=0;i<this.roster.length;i++)if(this.roster[i].id===id)return this.roster[i].name.toUpperCase();return null;},
    async load(){
      var r = await fetch('/api/core/state'); if(!r.ok) throw new Error('state '+r.status);
      return r.json();
    },
    async init(){
      var s; try{ s = await this.load(); } catch(e){ $('nc-sub').replaceChildren(); $('nc-sub').appendChild(subDot('warn')); $('nc-sub').appendChild(document.createTextNode(' COULD NOT REACH ATLAS. RETRYING.')); setTimeout(function(){live.init();},10000); return; }
      TZ = s.tz || TZ; greet();
      this.roster = s.roster || [];
      var byId = {}; (s.agents||[]).forEach(function(a){byId[a.id]=a;});
      makeAgents(this.roster.map(function(a){var st=byId[a.id]||{};var lbl=STATE_LABEL[st.state]||'IDLE';
        return {id:a.id,name:a.name.toUpperCase(),state:lbl,last:(st.last_action||'Nothing in the last day.')+(st.last_at?' '+st.last_at.slice(11,16)+'.':'')};}));
      feed.replaceChildren();
      var rec=(s.recent||[]).slice().reverse();
      if(!rec.length){var li=document.createElement('li');li.className='empty';var p=document.createElement('p');p.textContent='No hand-offs in the last day. When one agent asks another for something, it shows here.';li.appendChild(p);feed.appendChild(li);}
      rec.forEach(function(m){addFeed(m.from,m.to,m.summary,m.kind,0,(m.ts||'').slice(11,19),m.href);});
      this.apply(s);
      this.connect();
      clearInterval(this.refreshT); this.refreshT=setInterval(function(){live.refresh();},REFRESH_MS);
    },
    async refresh(){ try{ var s=await this.load(); this.apply(s);}catch(e){ /* the next tick tries again */ } },
    attention: [],
    apply(s){
      this.decisions = s.decisions || [];
      this.attention = (s.attention || []).filter(function(a){return a.id!=='decisions';});
      this.sub(s);
      if(!this.pending) this.render();
    },
    sub(s){
      var h=s.health||{},o=s.overnight||{done:0,spend:0},st={normal:'ALL SYSTEMS NORMAL',degraded:'SOMETHING NEEDS A LOOK',down:'ATLAS IS DOWN'}[h.status]||'STATUS UNKNOWN';
      var el=$('nc-sub');el.textContent='';el.appendChild(subDot(h.status==='normal'?'':'warn'));
      var money=' · OVERNIGHT $'+Number(o.spend||0).toFixed(2)+' · TODAY $'+Number(h.spend_today||0).toFixed(2)+' OF $'+Number(h.spend_cap||0).toFixed(0);
      el.appendChild(document.createTextNode(' '+AG.length+' AGENTS · '+o.done+' TASK'+(o.done===1?'':'S')+' DONE OVERNIGHT'+money+' · '+st));
      el.title=(h.reasons||[]).join('\n');
    },
    /* the other things that need a look, one row each, with where to go */
    renderLooks(){
      var box=$('nc-looks');box.replaceChildren();var items=this.attention;box.hidden=!items.length;
      items.forEach(function(a){var r=document.createElement('a');r.className='nc-look';r.href=a.href;
        var b=document.createElement('b');b.textContent=String(a.count);var t=document.createElement('span');t.textContent=a.label;
        r.appendChild(b);r.appendChild(t);box.appendChild(r);});
    },
    render(){
      var d=this.decisions,need=$('nc-need'),looks=this.attention.reduce(function(n,a){return n+a.count;},0);
      setHeadline(d.length,looks);this.renderLooks();
      root.classList.remove('authd');authorized=false;ring.style.setProperty('--p',0);
      $('nc-err').hidden=true;
      this.current=d[0]||null;
      if(!this.current){
        var clear=!this.attention.length;
        need.classList.toggle('clear',clear);setTag(clear?'ALL CLEAR':'NEEDS A LOOK');$('nc-exp').textContent='';
        $('nc-title').textContent=clear?'Nothing needs you':'No decisions waiting';
        $('nc-preview').textContent=clear?'When an agent needs your yes, it shows here first.':'Nothing waits on your yes. These still need you:';
        $('nc-hold').hidden=true;$('nc-second').hidden=true;
        AG.forEach(function(a){if(a.state==='WAITING ON YOU'){a.state='IDLE';a.base='IDLE';}});
        return;
      }
      need.classList.remove('clear');setTag('NEEDS YOUR YES');$('nc-hold').hidden=false;
      var c=this.current;
      $('nc-exp').textContent=c.expires_at?'EXPIRES '+c.expires_at.slice(11,16):'';
      $('nc-title').textContent=c.title;
      $('nc-preview').textContent=c.preview+(c.cost_estimate_usd!=null?' About $'+Number(c.cost_estimate_usd).toFixed(2)+'.':'');
      var two=d[1];$('nc-second').hidden=!two;
      if(two){$('nc-2t').textContent=two.title;$('nc-2s').textContent=(two.agent+' · '+(two.kind==='night_plan'?'NIGHT SHIFT':'APPROVAL')+(two.cost_estimate_usd!=null?' · ~$'+Number(two.cost_estimate_usd).toFixed(2):'')).toUpperCase();}
      var idx=agentIdx(String(c.agent).toUpperCase());if(idx>=0){AG[idx].state='WAITING ON YOU';AG[idx].base='WAITING ON YOU';}
    },
    key(){ return (window.crypto&&crypto.randomUUID)?crypto.randomUUID():String(Date.now())+Math.random(); },
    async decide(decision){
      var c=this.current; if(!c||this.pending) return;
      this.pending={id:c.id,decision:decision,key:this.key(),confirmed:false};
      var pend=this.pending;
      if(decision==='approve'){ /* optimistic */
        authorized=true;root.classList.add('authd');setTag('AUTHORIZED BY YOU');
        $('nc-sentb').textContent='Approving';$('nc-sents').textContent='Waiting for Atlas to confirm.';
      } else { $('nc-decline').disabled=true; }
      $('nc-auth').disabled=true;
      try{
        var r=await fetch('/api/core/decisions/'+encodeURIComponent(c.id)+'/decide',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({decision:decision,idempotency_key:pend.key})});
        var body=await r.json().catch(function(){return {};});
        if(!r.ok) throw new Error(body.detail||('Atlas answered '+r.status+'.'));
        /* the decision_changed event confirms; the answer itself does if the stream is slow */
        setTimeout(function(){ if(live.pending===pend && !pend.confirmed) live.confirmed(pend.id, body.status); }, 2500);
      }catch(e){
        this.pending=null; authorized=false; root.classList.remove('authd'); setTag('NEEDS YOUR YES');
        $('nc-auth').disabled=false; $('nc-decline').disabled=false; ring.style.setProperty('--p',0);
        var err=$('nc-err'); err.textContent=(decision==='approve'?'Not approved. ':'Not declined. ')+(e&&e.message?e.message:'Something went wrong.'); err.hidden=false;
      }
    },
    approve(){ this.decide('approve'); },
    decline(){ this.decide('decline'); },
    confirmed(id,status){
      var p=this.pending; if(!p||p.id!==id||p.confirmed) return;
      p.confirmed=true;
      if(p.decision==='approve'){
        var c=this.current||{};
        $('nc-sentb').textContent=status==='sent'?'Done':'Approved';
        $('nc-sents').textContent=c.kind==='night_plan'?'Tonight\'s tasks run on schedule.':((c.agent||'The agent')+' is running it now. It cannot be undone.');
      }
      setTimeout(function(){ live.pending=null; $('nc-auth').disabled=false; $('nc-decline').disabled=false; live.refresh(); }, p.decision==='approve'?3500:600);
    },
    connect(){
      if(this.es) this.es.close();
      var es=new EventSource('/api/core/events');this.es=es;
      var wasDown=false;
      es.onmessage=function(m){var ev;try{ev=JSON.parse(m.data);}catch(e){return;}live.onEvent(ev);};
      es.onerror=function(){wasDown=true;};
      es.onopen=function(){if(wasDown){wasDown=false;live.refresh();}};
    },
    onEvent(ev){
      if(ev.type==='message'){ this.queue.push(ev); return; }
      if(ev.type==='agent_state'){var n=this.nameOf(ev.agent),i=n?agentIdx(n):-1;if(i<0)return;
        var lbl=STATE_LABEL[ev.state]||'IDLE';AG[i].last=ev.last_action||AG[i].last;
        if(lbl==='RECEIVING'){AG[i].state=lbl;}else{AG[i].state=lbl;AG[i].base=lbl;}if(hov===i)showTip(i);return;}
      if(ev.type==='decision_changed'){
        if(this.pending&&this.pending.id===ev.id&&ev.status!=='pending')this.confirmed(ev.id,ev.status);
        clearTimeout(this._dt);this._dt=setTimeout(function(){if(!live.pending)live.refresh();},700);
      }
    },
    /* one packet start every 400 ms; a backlog plays six, the rest only reach the feed */
    pump(now){
      if(!this.queue.length||now-this.lastPacket<PACKET_GAP_MS)return;
      if(this.queue.length>BACKLOG_PACKETS){
        var extra=this.queue.splice(0,this.queue.length-BACKLOG_PACKETS);
        extra.forEach(function(m){addFeed(m.from,m.to,m.summary,m.kind,0,(m.ts||'').slice(11,19),m.href);});
      }
      var m=this.queue.shift();this.lastPacket=now;
      if(paused||reduce||hidden()){addFeed(m.from,m.to,m.summary,m.kind,0,(m.ts||'').slice(11,19),m.href);return;}
      send(m.from,m.to,m.summary,m.kind,tNow,null,(m.ts||'').slice(11,19),m.href);
    }
  };
  function subDot(cls){var i=document.createElement('i');i.textContent='●';if(cls)i.className=cls;return i;}

  /* ---------- the hold only works where it should ---------- */
  function shown(){return document.body.dataset.room==='bridge';}
  function hidden(){return document.hidden||!shown();}
  function typing(el){if(!el||el===document.body)return false;var t=(el.tagName||'').toLowerCase();
    if(t==='textarea'||t==='select')return true;if(t==='input')return!['button','checkbox','radio','range','submit','reset','file'].includes((el.type||'').toLowerCase());return!!el.isContentEditable;}
  function overlayOpen(){return !!document.querySelector('.veil:not([style*="display: none"])');}
  function canHold(){if(!shown()||typing(document.activeElement)||overlayOpen())return false;return DEMO?true:!!(live.current&&!live.pending);}

  /* ---------- quotes ---------- */
  var QS=[['The impediment to action advances action. What stands in the way becomes the way.','MARCUS AURELIUS'],
    ['Well done is better than well said.','BENJAMIN FRANKLIN'],
    ['The best way to predict the future is to invent it.','ALAN KAY'],
    ['We are what we repeatedly do.','WILL DURANT'],
    ['Fall seven times, stand up eight.','JAPANESE PROVERB'],
    ['A journey of a thousand miles begins with a single step.','LAOZI'],
    ['Energy and persistence conquer all things.','BENJAMIN FRANKLIN'],
    ['Make it work, make it right, make it fast.','KENT BECK'],
    ['Fortune favors the bold.','LATIN PROVERB'],
    ['Slow is smooth, smooth is fast.','MILITARY PROVERB']];
  var qe=[root.querySelector('.nc-q1'),root.querySelector('.nc-q2'),root.querySelector('.nc-q3')],qi=[0,5,2];
  function setQ(k){var q=QS[qi[k]%QS.length];qe[k].querySelector('p').textContent='“'+q[0]+'”';qe[k].querySelector('small').textContent=q[1];}
  [0,1,2].forEach(setQ);
  function cycle(k){qe[k].classList.add('off');setTimeout(function(){qi[k]+=2;setQ(k);qe[k].classList.remove('off');},1300);}
  function quotesOn(){try{return localStorage.getItem('atlas.quotes')!=='0';}catch(e){return true;}}
  function applyQuotes(){root.classList.toggle('noquotes',!quotesOn());}
  applyQuotes();document.addEventListener('atlas:quotes',applyQuotes);

  /* ---------- start ---------- */
  greet();
  if(DEMO){
    $('nc-demo').hidden=false;
    makeAgents(DEMO_AG.map(function(a){return {id:a[0].toLowerCase(),name:a[0],state:a[1],last:DEMO_LASTS[a[0]]};}));
    /* the reference's first frame: its seeded feed, its two decisions */
    addFeed('OPS','CTO','Nightly suite started. Waiting on the planner fix.','',-41);
    addFeed('RESEARCH','CLIENT DELIVERY','Helix Data thread summarized, 4 open questions.','',-96);
    setHeadline(2);setTag('NEEDS YOUR YES');$('nc-exp').textContent='EXPIRES 18:00';
    $('nc-title').textContent='Send the reply to Dana at Bayou Lending';
    $('nc-preview').textContent='Thank you for the updated payoff figures. I have attached the signed documents and confirmed the closing window for Friday.';
    $('nc-sentb').textContent='Authorized';$('nc-sents').textContent='COO is sending to Dana. Undo for 10 min.';$('nc-undo').hidden=false;
    $('nc-edit').hidden=false;
    $('nc-second').hidden=false;$('nc-2t').textContent='Tonight\'s night-shift plan';$('nc-2s').textContent='CTO · 3 TASKS · ~$0.40 · LOCAL ONLY';
    $('nc-sub').textContent='';$('nc-sub').appendChild(subDot(''));$('nc-sub').appendChild(document.createTextNode(' 8 AGENTS COORDINATING · 6 TASKS DONE OVERNIGHT · $1.71 · ALL SYSTEMS NORMAL'));
  } else {
    makeAgents([]);
    live.init();
  }
  $('nc-2b').addEventListener('click',function(){
    if(DEMO)return;var two=live.decisions[1];if(!two)return;
    location.hash=two.kind==='night_plan'?'#plan/night':'#approval/'+two.id;});

  if(reduce){ /* one composed still */
    var still=function(){for(var q=0;q<40;q++)fire(first[q%first.length],q*.05);
      /* the reference composes a packet into its still; live mode draws only what happened */
      if(DEMO)packets.push({a:0,b:1,t:.5,dur:1.7,label:AG[0].name+' → '+AG[1].name,kind:'',life:0});
      for(var z=0;z<90;z++)draw(20+z*.05,.05);};
    if(DEMO)still();else setTimeout(still,600);
    addEventListener('resize',function(){draw(tNow,0);});
    window.AtlasCore={bench:function(){return null;},live:live};
    return;
  }
  /* pause */
  var pb=$('nc-pause');
  pb.addEventListener('click',function(){paused=!paused;pb.textContent=paused?'RESUME':'PAUSE';pb.setAttribute('aria-pressed',paused?'true':'false');});
  /* hold the A key */
  addEventListener('keydown',function(e){if((e.key==='a'||e.key==='A')&&!e.repeat&&!e.ctrlKey&&!e.metaKey&&!e.altKey&&!authorized&&canHold()){hs(e);}});
  addEventListener('keyup',function(e){if(e.key==='a'||e.key==='A')he();});

  /* a new quote every minute (Trilok's ask, 2026-10-22). One quote on screen: the bottom one on a
     desk, the inline one on a phone; the top corner stays empty so the status line reads alone. */
  var QUOTE_MS=60000;
  if(!reduce){setInterval(function(){if(!paused&&!hidden()&&quotesOn()){cycle(1);cycle(2);}},QUOTE_MS);}

  /* the loop runs only while the room shows and the tab is visible */
  var prev=performance.now(),simT=0,raf=0;
  function loop(now){
    raf=0;
    if(hidden())return;
    var rdt=Math.min(.05,(now-prev)/1000);prev=now;
    if(holding&&!authorized){ring.style.setProperty('--p',Math.min(1,(now-holdStart)/900));}
    if(!DEMO)live.pump(now);
    if(!paused){simT+=rdt;draw(simT,rdt);}
    raf=requestAnimationFrame(loop);}
  function kick(){if(!raf&&!hidden()){prev=performance.now();layout();raf=requestAnimationFrame(loop);}}
  document.addEventListener('visibilitychange',kick);
  new MutationObserver(kick).observe(document.body,{attributes:true,attributeFilter:['data-room']});
  kick();

  /* where each agent's name lands, as draw() places it: the smoke test checks they stay on screen */
  function labels(){return AG.map(function(a){var ca=Math.cos(a.ang),sa=Math.sin(a.ang),lx=a.x+ca*24,ly=a.y+(sa<-.6?-36:(sa>.6?34:sa*14));
    var align=narrow?'center':(ca>.3?'left':(ca<-.3?'right':'center'));if(narrow){lx=a.x;ly=sa>.6?a.y+30:a.y-26;}
    var nm=narrow&&a.name==='CLIENT DELIVERY'?'CLIENT':a.name;ctx.font=(narrow?'500 12px':'500 13px')+' "Hanken Grotesk",system-ui,sans-serif';
    var w=ctx.measureText(nm).width,left=align==='left'?lx:(align==='right'?lx-w:lx-w/2);
    return {name:nm,left:left,right:left+w,top:ly-14,bottom:ly+14};});}
  /* frame cost at the current size: n frames drawn synchronously, with packets flying */
  window.AtlasCore={live:live,labels:labels,bench:function(n){n=n||240;var times=[],t=simT;
    /* bench packets go straight onto the canvas list, never into the feed */
    for(var f=0;f<n;f++){if(f%20===0&&AG.length>1){packets.push({a:f%AG.length,ap:null,b:(f+3)%AG.length,bp:null,t:0,dur:1.7,label:'BENCH',kind:'',life:0});burst(6);}
      var a=performance.now();t+=1/60;draw(t,1/60);times.push(performance.now()-a);}
    times.sort(function(x,y){return x-y;});
    return {frames:n,mean:+(times.reduce(function(s,x){return s+x;},0)/n).toFixed(2),p95:+times[Math.floor(n*.95)].toFixed(2),nodes:nodes.length,dpr:DPR,w:W,h:H};}};
  }

  whenReady(start);
})();
