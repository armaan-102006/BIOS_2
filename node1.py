import argparse
import asyncio
import hashlib
import json
import secrets
import socket
import time
from typing import Dict, List
import httpx
import uvicorn
from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

app = FastAPI()

# ==========================================
# 1. NETWORK & STATE INITIALIZATION
# ==========================================
def get_local_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
    except Exception:
        ip = "127.0.0.1"
    finally:
        s.close()
    return ip

NODE_STATE = {
    "node_id": "",
    "address": "",
    "mode": "honest",           # honest, liar, staller, colluder
    "strict_one_ip": True,      # Sybil Guard: 1 Vote per physical Laptop IP
    "peers": {},                # addr -> {"node_id": str, "trust": int, "banned": bool, "reason": str, "last_ans": str}
    "commits": {},              # task_key -> {"answer": str, "nonce": str, "primes": int, "sum": int}
    "job_results": [],
    "verdict": {
        "has_run": False,
        "total_primes": 0,
        "total_sum": 0,
        "verified_chunks": 0,
        "total_chunks": 8,
        "pbft_msgs": 0,
        "our_msgs": 0,
        "bandwidth_saved": "0%",
        "sybil_blocked": 0,
        "elapsed_sec": 0.0
    },
    "logs": []
}

def log_event(msg: str, level: str = "INFO"):
    timestamp = time.strftime("%H:%M:%S")
    NODE_STATE["logs"].insert(0, {"time": timestamp, "level": level, "msg": msg})
    NODE_STATE["logs"] = NODE_STATE["logs"][:60]
    print(f"[{timestamp}] [{level}] {msg}")

# ==========================================
# 2. STRICT 64-BIT INTEGER MATH ENGINE
# ==========================================
def solve_prime_chunk(start: int, end: int) -> dict:
    """Strictly deterministic integer state machine (zero float drift across CPU architectures)."""
    count = 0
    total_sum = 0
    for n in range(max(2, start), end + 1):
        is_p = True
        for i in range(2, int(n ** 0.5) + 1):
            if n % i == 0:
                is_p = False
                break
        if is_p:
            count += 1
            total_sum += n
    return {
        "primes": count,
        "sum": total_sum,
        "text": f"Primes: {count} | Sum: {total_sum}"
    }

# ==========================================
# 3. P2P MODELS & SYBIL ADMISSION GUARD
# ==========================================
class GossipPayload(BaseModel):
    sender_address: str
    sender_id: str
    known_peers: Dict[str, str]

class TaskRequest(BaseModel):
    job_id: str
    chunk_id: int
    start_num: int
    end_num: int

class ModeChange(BaseModel):
    mode: str

class AddPeerReq(BaseModel):
    peer_address: str

def is_sybil_duplicate_ip(candidate_addr: str) -> bool:
    """Checks if another port from the exact same physical IP is already in the swarm."""
    if not NODE_STATE["strict_one_ip"]:
        return False
    cand_ip = candidate_addr.split(":")[0]
    my_ip = NODE_STATE["address"].split(":")[0]
    if cand_ip == my_ip and candidate_addr != NODE_STATE["address"]:
        return True
    for existing_addr in NODE_STATE["peers"]:
        if existing_addr.split(":")[0] == cand_ip and existing_addr != candidate_addr:
            return True
    return False

async def execute_local_commit(req: TaskRequest) -> dict:
    mode = NODE_STATE["mode"]
    task_key = f"{req.job_id}_{req.chunk_id}"

    if mode == "staller":
        log_event(f"[STALLER] Hanging intentionally on Chunk #{req.chunk_id}...", "WARN")
        await asyncio.sleep(3.2)
        return {"node_address": NODE_STATE["address"], "stalled": True}

    real = solve_prime_chunk(req.start_num, req.end_num)
    if mode == "honest":
        ans_text = real["text"]
        p_cnt, p_sum = real["primes"], real["sum"]
    elif mode == "liar":
        fake_sum = secrets.randbelow(8000000) + 1000000
        ans_text = f"Primes: 9999 | Sum: {fake_sum} [FAKE]"
        p_cnt, p_sum = 9999, fake_sum
        log_event(f"[LIAR] Injecting fake answer for Chunk #{req.chunk_id}", "DANGER")
    else:  # colluder
        ans_text = "Primes: 7777 | Sum: 13371337 [COLLUDED]"
        p_cnt, p_sum = 7777, 13371337
        log_event(f"[COLLUDER] Sending coordinated team lie for Chunk #{req.chunk_id}", "DANGER")

    nonce = secrets.token_hex(8)
    NODE_STATE["commits"][task_key] = {
        "answer": ans_text,
        "nonce": nonce,
        "primes": p_cnt,
        "sum": p_sum
    }
    commit_hash = hashlib.sha256(f"{ans_text}|{nonce}".encode()).hexdigest()

    return {
        "node_address": NODE_STATE["address"],
        "node_id": NODE_STATE["node_id"],
        "commit_hash": commit_hash
    }

async def execute_local_reveal(req: TaskRequest) -> dict:
    task_key = f"{req.job_id}_{req.chunk_id}"
    saved = NODE_STATE["commits"].get(task_key)
    if not saved:
        return {"node_address": NODE_STATE["address"], "error": True}
    return {
        "node_address": NODE_STATE["address"],
        "node_id": NODE_STATE["node_id"],
        "answer": saved["answer"],
        "primes": saved["primes"],
        "sum": saved["sum"],
        "nonce": saved["nonce"]
    }

@app.post("/p2p/gossip")
async def receive_gossip(payload: GossipPayload):
    incoming = {payload.sender_address: payload.sender_id, **payload.known_peers}
    for addr, nid in incoming.items():
        if addr == NODE_STATE["address"]:
            continue
        if addr not in NODE_STATE["peers"]:
            if is_sybil_duplicate_ip(addr):
                NODE_STATE["verdict"]["sybil_blocked"] += 1
                log_event(f"🛡️ 1-IP-1-VOTE SYBIL GUARD: Blocked duplicate port {addr} from same physical laptop!", "DANGER")
                continue
            NODE_STATE["peers"][addr] = {
                "node_id": nid,
                "trust": 100,
                "banned": False,
                "reason": "Verified Honest",
                "last_ans": "Ready"
            }
            log_event(f"Connected to laptop peer: {addr}", "MESH")

    my_known = {NODE_STATE["address"]: NODE_STATE["node_id"]}
    for addr, info in NODE_STATE["peers"].items():
        if not info["banned"]:
            my_known[addr] = info["node_id"]
    return {"known_peers": my_known}

@app.post("/p2p/commit")
async def handle_commit(req: TaskRequest):
    return await execute_local_commit(req)

@app.post("/p2p/reveal")
async def handle_reveal(req: TaskRequest):
    return await execute_local_reveal(req)

# ==========================================
# 4. BFT CONSENSUS + VOTE BREAKDOWN ENGINE
# ==========================================
def pick_deterministic_quorum(job_id: str, chunk_id: int, active_nodes: List[dict], quorum_size: int):
    scored = []
    for n in active_nodes:
        h = hashlib.sha256(f"{job_id}_{chunk_id}_{n['address']}".encode()).hexdigest()
        scored.append((h, n))
    scored.sort(key=lambda x: x[0])
    return [item[1] for item in scored[:quorum_size]]

@app.post("/api/start-job")
async def start_distributed_job(shield_on: bool = True):
    t_start = time.time()
    job_id = secrets.token_hex(3)
    NODE_STATE["job_results"] = []

    total_primes_acc = 0
    total_sum_acc = 0
    verified_cnt = 0
    pbft_msgs = 0
    our_msgs = 0

    log_event(f"=== Starting Distributed Job #{job_id} (BFT Protection: {'ON' if shield_on else 'OFF'}) ===", "INFO")

    chunks = [(i + 1, i * 10000 + 1, (i + 1) * 10000) for i in range(8)]

    async with httpx.AsyncClient() as client:
        for chunk_id, start_n, end_n in chunks:
            active_nodes = [{"address": NODE_STATE["address"], "node_id": NODE_STATE["node_id"]}]
            for addr, info in NODE_STATE["peers"].items():
                if not info["banned"] or not shield_on:
                    active_nodes.append({"address": addr, "node_id": info["node_id"]})

            q_size = min(4, len(active_nodes)) if shield_on else 1
            assigned_nodes = pick_deterministic_quorum(job_id, chunk_id, active_nodes, q_size)

            # Calculate O(n^2) vs O(n) message complexity
            n_count = max(2, len(active_nodes))
            pbft_msgs += 2 * (n_count * n_count)
            our_msgs += 2 * q_size

            req_obj = TaskRequest(job_id=job_id, chunk_id=chunk_id, start_num=start_n, end_num=end_n)

            # PHASE 1: SEALED COMMIT ENVELOPES (2.2s Timeout)
            async def safe_ask_commit(target_addr):
                if target_addr == NODE_STATE["address"]:
                    try:
                        return await asyncio.wait_for(execute_local_commit(req_obj), timeout=2.2)
                    except Exception:
                        return {"node_address": target_addr, "stalled": True}
                try:
                    r = await client.post(f"http://{target_addr}/p2p/commit", json=req_obj.dict(), timeout=2.2)
                    return r.json()
                except Exception:
                    return {"node_address": target_addr, "stalled": True}

            commit_responses = await asyncio.gather(*(safe_ask_commit(n["address"]) for n in assigned_nodes))

            valid_commits = {}
            stalled_nodes = []
            vote_breakdown = []  # Side-by-side table rows for judges

            for c in commit_responses:
                c_addr = c.get("node_address")
                if c.get("stalled") or "commit_hash" not in c:
                    stalled_nodes.append(c_addr)
                    vote_breakdown.append({
                        "laptop": c_addr,
                        "answer": "⏱️ No Response (Timed Out > 2.2s)",
                        "verdict": "STALLED & BANNED" if shield_on else "STALLED"
                    })
                    if c_addr in NODE_STATE["peers"]:
                        NODE_STATE["peers"][c_addr]["last_ans"] = "⏱️ Timed Out (>2.2s)"
                        if shield_on:
                            NODE_STATE["peers"][c_addr]["trust"] = 0
                            NODE_STATE["peers"][c_addr]["banned"] = True
                            NODE_STATE["peers"][c_addr]["reason"] = f"Stalled on Chunk #{chunk_id}"
                            log_event(f"⏱️ STALLER CAUGHT: {c_addr} timed out on Chunk #{chunk_id} -> BANNED!", "WARN")
                else:
                    valid_commits[c_addr] = c["commit_hash"]

            # PHASE 2: REVEAL & OPEN ENVELOPES
            async def ask_reveal(target_addr):
                if target_addr == NODE_STATE["address"]:
                    return await execute_local_reveal(req_obj)
                try:
                    r = await client.post(f"http://{target_addr}/p2p/reveal", json=req_obj.dict(), timeout=2.0)
                    return r.json()
                except Exception:
                    return {"node_address": target_addr, "error": True}

            reveal_responses = await asyncio.gather(*(ask_reveal(addr) for addr in valid_commits.keys()))

            votes: Dict[str, List[str]] = {}
            ans_meta: Dict[str, dict] = {}

            for rev in reveal_responses:
                addr = rev.get("node_address")
                ans = rev.get("answer")
                nonce = rev.get("nonce")
                if not ans or not nonce:
                    continue
                if hashlib.sha256(f"{ans}|{nonce}".encode()).hexdigest() == valid_commits.get(addr):
                    votes.setdefault(ans, []).append(addr)
                    ans_meta[ans] = {"primes": rev.get("primes", 0), "sum": rev.get("sum", 0)}

            # PHASE 3: COMPARE VOTES & BAN LIARS/COLLUDERS
            if not votes:
                winning_ans = "FAILED (Node Stalled)"
                status = "STALLED"
                liars_caught = []
            else:
                sorted_votes = sorted(votes.items(), key=lambda x: len(x[1]), reverse=True)
                winning_ans, winners = sorted_votes[0]
                liars_caught = []

                # Record winning voters in table
                for w_addr in winners:
                    is_corrupt = ("FAKE" in winning_ans or "COLLUDED" in winning_ans)
                    vote_breakdown.append({
                        "laptop": w_addr,
                        "answer": winning_ans,
                        "verdict": "UNVERIFIED LIE ACCEPTED" if is_corrupt else f"MAJORITY WINNER ({len(winners)} Votes) ✅"
                    })
                    if w_addr in NODE_STATE["peers"]:
                        NODE_STATE["peers"][w_addr]["last_ans"] = winning_ans

                # Record & Ban outvoted Liars / Colluders
                for ans_val, voters in sorted_votes[1:]:
                    for l_addr in voters:
                        liars_caught.append(l_addr)
                        lie_type = "COLLUDER" if "COLLUDED" in ans_val else "LIAR"
                        vote_breakdown.append({
                            "laptop": l_addr,
                            "answer": ans_val,
                            "verdict": f"CAUGHT {lie_type} & BANNED 🚨"
                        })
                        if l_addr in NODE_STATE["peers"]:
                            NODE_STATE["peers"][l_addr]["last_ans"] = ans_val
                            if shield_on:
                                NODE_STATE["peers"][l_addr]["trust"] = 0
                                NODE_STATE["peers"][l_addr]["banned"] = True
                                NODE_STATE["peers"][l_addr]["reason"] = f"Caught {lie_type} on Chunk #{chunk_id}"
                                log_event(f"🚨 {lie_type} CAUGHT: {l_addr} answered '{ans_val}' vs Majority '{winning_ans}' -> BANNED!", "DANGER")

                status = "VERIFIED" if ("FAKE" not in winning_ans and "COLLUDED" not in winning_ans) else "CORRUPTED"
                if status == "VERIFIED":
                    verified_cnt += 1
                    total_primes_acc += ans_meta[winning_ans]["primes"]
                    total_sum_acc += ans_meta[winning_ans]["sum"]

            NODE_STATE["job_results"].append({
                "chunk_id": chunk_id,
                "range": f"{start_n:,} – {end_n:,}",
                "winning_answer": winning_ans,
                "status": status,
                "vote_breakdown": vote_breakdown
            })

    saved_pct = int(round((1 - (our_msgs / max(1, pbft_msgs))) * 100))
    NODE_STATE["verdict"] = {
        "has_run": True,
        "total_primes": total_primes_acc,
        "total_sum": total_sum_acc,
        "verified_chunks": verified_cnt,
        "total_chunks": 8,
        "pbft_msgs": pbft_msgs,
        "our_msgs": our_msgs,
        "bandwidth_saved": f"{saved_pct}% Less Chatter",
        "sybil_blocked": NODE_STATE["verdict"]["sybil_blocked"],
        "elapsed_sec": round(time.time() - t_start, 2)
    }
    return {"status": "completed", "verdict": NODE_STATE["verdict"], "results": NODE_STATE["job_results"]}

# ==========================================
# 5. API CONTROLS & CLEAN JUDGE UI
# ==========================================
@app.get("/api/state")
async def get_state():
    return NODE_STATE

@app.post("/api/mode")
async def set_mode(req: ModeChange):
    NODE_STATE["mode"] = req.mode
    log_event(f"Switched THIS laptop to: {req.mode.upper()}", "WARN")
    return {"mode": NODE_STATE["mode"]}

@app.post("/api/toggle-sybil-guard")
async def toggle_sybil():
    NODE_STATE["strict_one_ip"] = not NODE_STATE["strict_one_ip"]
    state_txt = "ON (1 Vote Per Physical Laptop IP)" if NODE_STATE["strict_one_ip"] else "OFF (Multi-Port Allowed)"
    log_event(f"1-IP-1-Vote Sybil Guard is now: {state_txt}", "INFO")
    return {"strict_one_ip": NODE_STATE["strict_one_ip"]}

@app.post("/api/test-sybil-clone")
async def test_sybil_clone():
    """Simulates a cheater laptop trying to spin up an extra port on the same IP."""
    my_ip = NODE_STATE["address"].split(":")[0]
    fake_clone = f"{my_ip}:8099"
    if is_sybil_duplicate_ip(fake_clone):
        NODE_STATE["verdict"]["sybil_blocked"] += 1
        log_event(f"🛡️ SYBIL ATTACK BLOCKED: {fake_clone} rejected! IP {my_ip} already has 1 active vote.", "DANGER")
    else:
        log_event(f"⚠️ Sybil Guard is OFF: {fake_clone} would be allowed.", "WARN")
    return {"status": "tested"}

@app.post("/api/add-peer")
async def manual_add_peer(req: AddPeerReq):
    addr = req.peer_address.replace("http://", "").replace("/", "").strip()
    if addr and addr != NODE_STATE["address"]:
        if is_sybil_duplicate_ip(addr):
            NODE_STATE["verdict"]["sybil_blocked"] += 1
            log_event(f"🛡️ SYBIL GUARD: Blocked {addr} (Duplicate Laptop IP)!", "DANGER")
            return {"status": "blocked"}
        await sync_with_peer(addr)
    return {"status": "ok"}

@app.post("/api/reset-trust")
async def reset_trust():
    my_subnet = ".".join(NODE_STATE["address"].split(".")[:2])
    ghosts = [a for a in NODE_STATE["peers"] if not a.startswith(my_subnet)]
    for g in ghosts:
        del NODE_STATE["peers"][g]
    for addr in NODE_STATE["peers"]:
        NODE_STATE["peers"][addr]["trust"] = 100
        NODE_STATE["peers"][addr]["banned"] = False
        NODE_STATE["peers"][addr]["reason"] = "Verified Honest"
        NODE_STATE["peers"][addr]["last_ans"] = "Ready"
    log_event("Reset all laptops to 100% Trusted & cleared ghost IPs.", "INFO")
    return {"status": "ok"}

async def sync_with_peer(peer_addr: str):
    if peer_addr == NODE_STATE["address"]:
        return
    try:
        async with httpx.AsyncClient() as client:
            my_known = {NODE_STATE["address"]: NODE_STATE["node_id"]}
            for a, info in NODE_STATE["peers"].items():
                if not info["banned"]:
                    my_known[a] = info["node_id"]
            r = await client.post(
                f"http://{peer_addr}/p2p/gossip",
                json={"sender_address": NODE_STATE["address"], "sender_id": NODE_STATE["node_id"], "known_peers": my_known},
                timeout=1.5
            )
            data = r.json()
            for addr, nid in data.get("known_peers", {}).items():
                if addr != NODE_STATE["address"] and addr not in NODE_STATE["peers"]:
                    if not is_sybil_duplicate_ip(addr):
                        NODE_STATE["peers"][addr] = {
                            "node_id": nid,
                            "trust": 100,
                            "banned": False,
                            "reason": "Verified Honest",
                            "last_ans": "Ready"
                        }
    except Exception:
        pass

async def udp_autodiscovery_loop():
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.bind(("", 9998))
        sock.setblocking(False)
    except Exception:
        pass

    while True:
        await asyncio.sleep(2.0)
        try:
            msg = json.dumps({"addr": NODE_STATE["address"], "id": NODE_STATE["node_id"]}).encode()
            sock.sendto(msg, ("255.255.255.255", 9998))
        except Exception:
            pass
        while True:
            try:
                data, _ = sock.recvfrom(1024)
                info = json.loads(data.decode())
                p_addr = info.get("addr")
                if p_addr and p_addr != NODE_STATE["address"] and p_addr not in NODE_STATE["peers"]:
                    if not is_sybil_duplicate_ip(p_addr):
                        NODE_STATE["peers"][p_addr] = {
                            "node_id": info.get("id", "peer"),
                            "trust": 100,
                            "banned": False,
                            "reason": "Verified Honest",
                            "last_ans": "Ready"
                        }
                        log_event(f"📡 Auto-discovered laptop on Hotspot: {p_addr}", "MESH")
            except Exception:
                break

async def background_gossip_loop():
    while True:
        await asyncio.sleep(2.5)
        for addr in list(NODE_STATE["peers"].keys()):
            if not NODE_STATE["peers"][addr]["banned"]:
                await sync_with_peer(addr)

@app.on_event("startup")
async def on_startup():
    asyncio.create_task(background_gossip_loop())
    asyncio.create_task(udp_autodiscovery_loop())

@app.get("/", response_class=HTMLResponse)
async def dashboard():
    return """
<!DOCTYPE html>
<html>
<head>
  <title>The Liar's Swarm — Executive Verdict Dashboard</title>
  <script src="https://cdn.tailwindcss.com"></script>
</head>
<body class="bg-slate-950 text-slate-100 min-h-screen p-5 font-sans">
  <div class="max-w-6xl mx-auto space-y-5">

    <!-- 1. TOP BAR: THIS LAPTOP & BEHAVIOR CONTROLS -->
    <div class="bg-slate-900 border border-slate-800 rounded-2xl p-4 flex flex-wrap items-center justify-between gap-4">
      <div>
        <div class="text-[11px] uppercase tracking-widest text-indigo-400 font-bold">The Liar's Swarm • Leaderless P2P Node</div>
        <h1 class="text-2xl font-bold font-mono mt-0.5" id="myAddress">Loading...</h1>
      </div>
      <div class="flex flex-wrap items-center gap-2">
        <span class="text-xs text-slate-400 mr-1">Set THIS Laptop's Role:</span>
        <button onclick="setMode('honest')" id="btn-honest" class="px-3.5 py-1.5 rounded-lg text-xs font-bold">HONEST (Truth)</button>
        <button onclick="setMode('liar')" id="btn-liar" class="px-3.5 py-1.5 rounded-lg text-xs font-bold">LIAR (Fake Math)</button>
        <button onclick="setMode('staller')" id="btn-staller" class="px-3.5 py-1.5 rounded-lg text-xs font-bold">STALLER (Freeze)</button>
        <button onclick="setMode('colluder')" id="btn-colluder" class="px-3.5 py-1.5 rounded-lg text-xs font-bold">COLLUDER (Team Lie)</button>
      </div>
    </div>

    <!-- 2. EXECUTIVE VERDICT BANNER (THE FINAL ANSWER + RESEARCH ADVANTAGES) -->
    <div class="grid grid-cols-1 md:grid-cols-3 gap-4">
      <div class="bg-slate-900 border border-emerald-800/60 rounded-2xl p-4">
        <div class="text-[11px] uppercase font-bold text-emerald-400">Final Verified Math Answer (1 to 80,000)</div>
        <div class="text-xl font-bold font-mono text-white mt-1" id="verdictAnswer">7,837 Primes | Sum: 303,791,334</div>
        <div class="text-xs text-slate-400 mt-1" id="verdictStatus">Ready — Click "Run WITH Protection" below</div>
      </div>

      <div class="bg-slate-900 border border-slate-800 rounded-2xl p-4">
        <div class="text-[11px] uppercase font-bold text-indigo-400">O(n) Collector vs Classic PBFT O(n²)</div>
        <div class="text-xl font-bold font-mono text-cyan-400 mt-1" id="verdictMsgs">75% Less Wi-Fi Chatter</div>
        <div class="text-xs text-slate-400 mt-1" id="verdictMsgDetail">Nodes send sealed commits to collector, not all-to-all</div>
      </div>

      <div class="bg-slate-900 border border-slate-800 rounded-2xl p-4 flex justify-between items-center">
        <div>
          <div class="text-[11px] uppercase font-bold text-rose-400">1-IP-1-Vote Sybil Guard</div>
          <div class="text-xl font-bold font-mono text-rose-300 mt-1" id="verdictSybil">0 Clone Ports Blocked</div>
          <div class="text-xs text-slate-400 mt-1" id="sybilStatusText">1 Physical Laptop = 1 Vote</div>
        </div>
        <div class="flex flex-col gap-1.5">
          <button onclick="testSybil()" class="bg-rose-950 hover:bg-rose-900 border border-rose-700 text-rose-200 px-3 py-1.5 rounded-lg text-xs font-bold">Simulate Fake Port</button>
          <button onclick="toggleSybil()" class="text-[10px] text-slate-400 hover:underline text-right" id="toggleSybilBtn">Toggle Guard</button>
        </div>
      </div>
    </div>

    <!-- 3. LIVE LAPTOP HEALTH SCOREBOARD (SHOWS WHICH LAPTOP IS HONEST VS FAULTY) -->
    <div class="bg-slate-900 border border-slate-800 rounded-2xl p-4 space-y-3">
      <div class="flex flex-wrap justify-between items-center gap-2">
        <div>
          <h2 class="text-sm font-bold uppercase tracking-wider text-slate-200">1. Connected Laptops Health & Fault Verdict</h2>
          <p class="text-xs text-slate-400">Shows all laptops in the swarm and automatically flags Liars, Stallers, and Colluders.</p>
        </div>
        <div class="flex gap-2">
          <input id="peerInput" placeholder="Add Laptop IP:Port" class="bg-slate-950 border border-slate-800 rounded-lg px-3 py-1.5 text-xs font-mono"/>
          <button onclick="addPeer()" class="bg-indigo-600 hover:bg-indigo-500 px-3 py-1.5 rounded-lg text-xs font-bold">Connect</button>
          <button onclick="resetTrust()" class="bg-slate-800 hover:bg-slate-700 px-3 py-1.5 rounded-lg text-xs font-bold text-indigo-300">Unban All & Reset</button>
        </div>
      </div>

      <div id="laptopGrid" class="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-3"></div>
    </div>

    <!-- 4. SIDE-BY-SIDE MAJORITY VOTE BREAKDOWN PER CHUNK -->
    <div class="bg-slate-900 border border-slate-800 rounded-2xl p-4 space-y-4">
      <div class="flex flex-wrap justify-between items-center gap-3 border-b border-slate-800 pb-3">
        <div>
          <h2 class="text-sm font-bold uppercase tracking-wider text-slate-200">2. Side-by-Side Laptop Vote Comparison (8 Chunks)</h2>
          <p class="text-xs text-slate-400">Compare what each laptop answered inside its sealed Commit-Reveal envelope.</p>
        </div>
        <div class="flex gap-2">
          <button onclick="runJob(true)" class="bg-emerald-600 hover:bg-emerald-500 px-4 py-2 rounded-xl text-xs font-bold shadow-lg">▶ Run WITH BFT Protection</button>
          <button onclick="runJob(false)" class="bg-rose-700 hover:bg-rose-600 px-3 py-2 rounded-xl text-xs font-bold">Run WITHOUT Protection</button>
        </div>
      </div>

      <div id="chunkList" class="grid grid-cols-1 md:grid-cols-2 gap-3">
        <div class="col-span-2 text-slate-500 text-xs py-10 text-center">
          Click <b>"▶ Run WITH BFT Protection"</b> to see every laptop's vote compared side-by-side.
        </div>
      </div>
    </div>

    <!-- 5. SECURITY LOG -->
    <div class="bg-slate-900 border border-slate-800 rounded-2xl p-4">
      <h2 class="text-xs font-bold uppercase text-slate-400 mb-2">3. Live Security Event Log</h2>
      <div id="logBox" class="bg-slate-950 rounded-xl p-3 font-mono text-xs space-y-1 h-32 overflow-y-auto border border-slate-800"></div>
    </div>

  </div>

  <script>
    async function refresh() {
      const res = await fetch('/api/state');
      const s = await res.json();
      document.getElementById('myAddress').innerText = s.address;

      ['honest','liar','staller','colluder'].forEach(m => {
        document.getElementById('btn-' + m).className = s.mode === m
          ? 'px-3.5 py-1.5 rounded-lg text-xs font-bold bg-indigo-600 text-white shadow'
          : 'px-3.5 py-1.5 rounded-lg text-xs font-bold bg-slate-950 text-slate-400 border border-slate-800';
      });

      // Executive Verdict Bar
      if (s.verdict && s.verdict.has_run) {
        document.getElementById('verdictAnswer').innerText = `${s.verdict.total_primes.toLocaleString()} Primes | Sum: ${s.verdict.total_sum.toLocaleString()}`;
        document.getElementById('verdictStatus').innerText = `${s.verdict.verified_chunks}/${s.verdict.total_chunks} Chunks Verified in ${s.verdict.elapsed_sec}s`;
        document.getElementById('verdictMsgs').innerText = `${s.verdict.bandwidth_saved} (${s.verdict.our_msgs} vs ${s.verdict.pbft_msgs} msgs)`;
      }
      document.getElementById('verdictSybil').innerText = `${s.verdict.sybil_blocked} Clone Ports Blocked`;
      document.getElementById('sybilStatusText').innerText = s.strict_one_ip ? 'Guard ON: 1 IP = 1 Vote' : 'Guard OFF: Multi-Port Allowed';

      // Build 4 Laptop Cards (Self + Connected Peers)
      const allLaptops = [
        {
          addr: s.address + ' (THIS PC)',
          banned: false,
          reason: 'Current Mode: ' + s.mode.toUpperCase(),
          last_ans: 'Active Coordinator'
        },
        ...Object.entries(s.peers).map(([addr, info]) => ({
          addr,
          banned: info.banned,
          reason: info.reason || 'Verified Honest',
          last_ans: info.last_ans || 'Ready'
        }))
      ];

      document.getElementById('laptopGrid').innerHTML = allLaptops.map(l => `
        <div class="p-3.5 rounded-xl bg-slate-950 border ${l.banned ? 'border-rose-600 bg-rose-950/20' : 'border-emerald-800/60'} flex flex-col justify-between gap-2">
          <div class="flex justify-between items-start gap-2">
            <span class="font-mono font-bold text-xs text-white break-all">${l.addr}</span>
            <span class="px-2 py-0.5 rounded text-[10px] font-bold shrink-0 ${l.banned ? 'bg-rose-600 text-white' : 'bg-emerald-950 text-emerald-400 border border-emerald-700'}">
              ${l.banned ? 'FAULTY • BANNED 🚨' : 'TRUSTED ✅'}
            </span>
          </div>
          <div class="text-[11px] ${l.banned ? 'text-rose-300 font-bold' : 'text-slate-400'}">${l.reason}</div>
          <div class="text-[10px] font-mono text-slate-500 truncate">Last Vote: ${l.last_ans}</div>
        </div>
      `).join('');

      document.getElementById('logBox').innerHTML = s.logs.map(l =>
        `<div class="${l.level==='DANGER'?'text-rose-400':l.level==='WARN'?'text-amber-400':l.level==='MESH'?'text-cyan-400':'text-slate-300'}">[${l.time}] ${l.msg}</div>`
      ).join('');
    }

    async function setMode(mode) { await fetch('/api/mode',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({mode})}); refresh(); }
    async function addPeer() {
      const val = document.getElementById('peerInput').value.trim();
      if (!val) return;
      await fetch('/api/add-peer',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({peer_address:val})});
      document.getElementById('peerInput').value = '';
      refresh();
    }
    async function resetTrust() { await fetch('/api/reset-trust',{method:'POST'}); refresh(); }
    async function testSybil() { await fetch('/api/test-sybil-clone',{method:'POST'}); refresh(); }
    async function toggleSybil() { await fetch('/api/toggle-sybil-guard',{method:'POST'}); refresh(); }

    async function runJob(shieldOn) {
      document.getElementById('chunkList').innerHTML = '<div class="col-span-2 text-indigo-400 text-xs py-10 text-center animate-pulse font-bold">Collecting Sealed Envelopes & Comparing Laptop Votes across Hotspot...</div>';
      const res = await fetch('/api/start-job?shield_on=' + shieldOn, {method:'POST'});
      const data = await res.json();

      document.getElementById('chunkList').innerHTML = data.results.map(r => `
        <div class="p-3.5 rounded-xl bg-slate-950 border ${r.status==='VERIFIED'?'border-slate-800':'border-rose-600'} text-xs space-y-2">
          <div class="flex justify-between items-center border-b border-slate-800 pb-2">
            <span class="font-bold text-slate-200">Chunk #${r.chunk_id} (${r.range})</span>
            <span class="px-2 py-0.5 rounded text-[10px] font-bold ${r.status==='VERIFIED'?'bg-emerald-950 text-emerald-400':'bg-rose-950 text-rose-400'}">${r.status}</span>
          </div>
          <div class="font-mono font-bold text-emerald-300 text-xs">Winning Majority: ${r.winning_answer}</div>
          <div class="space-y-1 pt-1">
            ${r.vote_breakdown.map(v => `
              <div class="flex justify-between items-center text-[11px] bg-slate-900/90 px-2.5 py-1 rounded border border-slate-800/80">
                <span class="font-mono text-slate-300">${v.laptop}</span>
                <span class="font-mono text-slate-400">${v.answer}</span>
                <span class="font-bold ${v.verdict.includes('WINNER')?'text-emerald-400':v.verdict.includes('STALLED')?'text-amber-400':'text-rose-400'}">${v.verdict}</span>
              </div>
            `).join('')}
          </div>
        </div>
      `).join('');
      refresh();
    }

    setInterval(refresh, 1500);
    refresh();
  </script>
</body>
</html>
"""

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--ip", type=str, default=None)
    parser.add_argument("--peer", type=str, default=None)
    parser.add_argument("--mode", type=str, default="honest", choices=["honest", "liar", "staller", "colluder"])
    args = parser.parse_args()

    my_ip = args.ip if args.ip else get_local_ip()
    NODE_STATE["address"] = f"{my_ip}:{args.port}"
    NODE_STATE["node_id"] = hashlib.sha256(NODE_STATE["address"].encode()).hexdigest()[:12]
    NODE_STATE["mode"] = args.mode

    if args.peer:
        clean_peer = args.peer.replace("http://", "").replace("/", "").strip()
        NODE_STATE["peers"][clean_peer] = {
            "node_id": "connecting...",
            "trust": 100,
            "banned": False,
            "reason": "Verified Honest",
            "last_ans": "Ready"
        }

    log_event(f"Node live at http://{NODE_STATE['address']} ({args.mode.upper()})")
    uvicorn.run(app, host="0.0.0.0", port=args.port, log_level="warning")