# main.py
import asyncio
import uuid
import networkx as nx
import numpy as np
from fastapi import FastAPI, WebSocket, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

app = FastAPI()
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"])

# 15 stations with coordinates
GRAPH_NODES = {
    "S1": (100, 100),  "S2": (260, 60),  "S3": (420, 80),
    "S4": (600, 100),  "S5": (680, 250), "S6": (620, 400),
    "S7": (400, 360),  "S8": (230, 350), "S9": (110, 250),
    "S10": (330, 180), "S11": (520, 250), "S12": (550, 350),
    "S13": (720, 320), "S14": (140, 170), "S15": (700, 450)
}
GRAPH_EDGES = [
    ("S1","S2"), ("S2","S3"), ("S3","S4"), ("S4","S5"),
    ("S5","S6"), ("S6","S7"), ("S7","S8"), ("S8","S9"), ("S9","S1"),
    ("S3","S10"), ("S10","S7"), ("S2","S10"), ("S10","S8"),
    ("S5","S11"), ("S11","S12"), ("S12","S6"), ("S11","S13"),
    ("S9","S14"), ("S13","S15"), ("S6","S15")
]
G = nx.Graph()
for u, v in GRAPH_EDGES:
    G.add_edge(u, v, weight=1)

# State
TRAIN = None
RESERVATIONS = {}
CLIENTS = set()

def edge_key(u, v):
    return tuple(sorted((u, v)))

def reserved_count_for_edge(edge, start_time, duration_steps):
    cnt = 0
    for t in range(start_time, start_time + duration_steps):
        if (edge, t) in RESERVATIONS:
            cnt += 1
    return cnt

def build_congestion_aware_graph(start_time, per_edge_steps=16, alpha=2.0):
    H = nx.Graph()
    for u, v, data in G.edges(data=True):
        base = data.get("weight", 1)
        edge = edge_key(u, v)
        density = reserved_count_for_edge(edge, start_time, per_edge_steps)
        H.add_edge(u, v, weight=base + alpha * density)
    return H

async def simulate_train(train):
    t_step = train["start_time"]
    path = train["path"]
    src = path[0]
    await asyncio.sleep(0)  # no delay now
    for i in range(len(path) - 1):
        u, v = path[i], path[i + 1]
        u_pos, v_pos = GRAPH_NODES[u], GRAPH_NODES[v]
        for step in range(16):
            x = u_pos[0] + (v_pos[0] - u_pos[0]) * (step / 16)
            y = u_pos[1] + (v_pos[1] - u_pos[1]) * (step / 16)
            train["position"] = (x, y)
            train["status"] = f"moving {u}->{v}"
            await broadcast({"type": "train_update", "train": train})
            await asyncio.sleep(0.2)
        t_step += 16
    train["status"] = "arrived"
    train["position"] = GRAPH_NODES[path[-1]]
    await broadcast({"type": "train_update", "train": train})

@app.post("/schedule_train")
async def schedule_train(req: Request):
    try:
        t = await req.json()
    except Exception as e:
        return JSONResponse({"error": "invalid json body", "detail": str(e)}, status_code=400)
    src = t.get("source")
    dst = t.get("destination")
    if src not in GRAPH_NODES or dst not in GRAPH_NODES or src == dst:
        return JSONResponse({"ok": False, "error": "invalid source/destination"})
    PER_EDGE_STEPS = 16
    MAX_RETRY = 1200
    start_time = 0
    attempts, found_path, found_start = 0, None, None
    while attempts < MAX_RETRY:
        H = build_congestion_aware_graph(start_time, per_edge_steps=PER_EDGE_STEPS)
        try:
            candidate = nx.shortest_path(H, source=src, target=dst, weight="weight")
        except (nx.NetworkXNoPath, nx.NodeNotFound):
            candidate = None
        if candidate and len(candidate) >= 2:
            tcur = start_time
            collision = False
            for i in range(len(candidate) - 1):
                edge = edge_key(candidate[i], candidate[i + 1])
                for s in range(PER_EDGE_STEPS):
                    if (edge, tcur) in RESERVATIONS:
                        collision = True
                        break
                    tcur += 1
                if collision:
                    break
            if not collision:
                tcur = start_time
                for i in range(len(candidate) - 1):
                    edge = edge_key(candidate[i], candidate[i + 1])
                    for s in range(PER_EDGE_STEPS):
                        RESERVATIONS[(edge, tcur)] = "T"
                        tcur += 1
                found_path, found_start = candidate, start_time
                break
        start_time += PER_EDGE_STEPS
        attempts += 1
    if not found_path:
        return JSONResponse({"ok": False, "error": "could not find free path"})
    global TRAIN
    TRAIN = {
        "id": str(uuid.uuid4())[:6],
        "path": found_path,
        "position": GRAPH_NODES[src],
        "status": "scheduled",
        "start_time": found_start if found_start is not None else 0,
    }
    asyncio.create_task(simulate_train(TRAIN.copy()))
    return JSONResponse({
        "ok": True,
        "train_id": TRAIN["id"],
        "path": found_path,
        "start_time": TRAIN["start_time"],
    })

async def broadcast(msg: dict):
    dead = []
    for ws in list(CLIENTS):
        try:
            await ws.send_json(msg)
        except Exception:
            dead.append(ws)
    for d in dead:
        CLIENTS.discard(d)

@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    await ws.accept()
    CLIENTS.add(ws)
    await ws.send_json({"type": "hello", "graph": True})
    try:
        while True:
            await ws.send_json({"type": "train_update", "train": TRAIN if TRAIN else {}})
            await asyncio.sleep(1)
    except Exception:
        CLIENTS.discard(ws)
