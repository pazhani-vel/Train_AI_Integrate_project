import asyncio
import uuid
import networkx as nx
from fastapi import FastAPI, WebSocket, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

app = FastAPI()

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------------- GRAPH SETUP ----------------
G = nx.Graph()
GRAPH_NODES = {
    "A": (100, 100),
    "B": (300, 100),
    "C": (500, 200),
    "D": (300, 300),
    "E": (100, 300),
}
GRAPH_EDGES = [("A", "B"), ("B", "C"), ("B", "D"), ("D", "E"), ("A", "E")]
for u, v in GRAPH_EDGES:
    G.add_edge(u, v, weight=1)

# ---------------- TRAIN STATE ----------------
TRAINS = {}
RESERVATIONS = {}  # (edge, time_step) -> train_id
CLIENTS = set()

# ---------------- GRAPH ENDPOINT ----------------
@app.get("/graph")
async def get_graph():
    return {
        "nodes": {k: {"pos": v} for k, v in GRAPH_NODES.items()},
        "edges": [{"u": u, "v": v} for u, v in GRAPH_EDGES],
    }

# ---------------- RESERVATION LOGIC ----------------
def is_path_free(path, start_time=0):
    t = start_time
    for i in range(len(path) - 1):
        edge = tuple(sorted((path[i], path[i + 1])))
        for step in range(20):  # simulate time steps along edge
            if (edge, t) in RESERVATIONS:
                return False
            t += 1
    return True

def reserve_path(path, train_id, start_time=0):
    t = start_time
    for i in range(len(path) - 1):
        edge = tuple(sorted((path[i], path[i + 1])))
        for step in range(20):
            RESERVATIONS[(edge, t)] = train_id
            t += 1

# ---------------- TRAIN SIMULATION ----------------
async def simulate_train(train):
    await asyncio.sleep(train["delay"])  # initial departure delay
    path = train["path"]
    t_step = train["start_time"]

    for i in range(len(path) - 1):
        u, v = path[i], path[i + 1]
        u_pos, v_pos = GRAPH_NODES[u], GRAPH_NODES[v]

        for step in range(20):
            x = u_pos[0] + (v_pos[0] - u_pos[0]) * (step / 20)
            y = u_pos[1] + (v_pos[1] - u_pos[1]) * (step / 20)
            train["position"] = (x, y)
            train["status"] = f"moving {u}->{v}"
            await broadcast({"type": "train_update", "train": train})
            await asyncio.sleep(0.3)  # adjust speed
        t_step += 20

    train["status"] = "arrived"
    train["position"] = GRAPH_NODES[path[-1]]
    await broadcast({"type": "train_update", "train": train})

# ---------------- SCHEDULE BATCH ----------------
@app.post("/schedule_batch")
async def schedule_batch(req: Request):
    trains_req = await req.json()
    # sort by priority (lower number = higher priority)
    trains_req.sort(key=lambda t: t.get("priority", 5))
    results = []

    for t in trains_req:
        src, dst = t["source"], t["destination"]
        tid = str(uuid.uuid4())[:6]
        delay = int(t.get("delay", 0))
        priority = int(t.get("priority", 5))

        # generate all paths sorted by shortest path first
        paths = list(nx.all_simple_paths(G, src, dst))
        paths.sort(key=lambda p: nx.path_weight(G, p, "weight"))

        found_path = None
        start_time = 0

        # assign first available path that is free in RESERVATIONS
        while True:
            for p in paths:
                if is_path_free(p, start_time):
                    found_path = p
                    reserve_path(p, tid, start_time)
                    break
            if found_path:
                break
            start_time += 20  # wait and retry

        train = {
            "id": tid,
            "path": found_path,
            "position": GRAPH_NODES[src],
            "status": "waiting (delayed)" if delay > 0 else "scheduled",
            "start_time": start_time,
            "delay": delay,
            "priority": priority
        }
        TRAINS[tid] = train
        asyncio.create_task(simulate_train(train))
        results.append({"train_id": tid, "ok": True, "path": found_path})

    return JSONResponse({"results": results})

# ---------------- WEBSOCKET ----------------
async def broadcast(msg: dict):
    dead = []
    for ws in CLIENTS:
        try:
            await ws.send_json(msg)
        except Exception:
            dead.append(ws)
    for d in dead:
        CLIENTS.remove(d)

@app.websocket("/ws")
async def ws(ws: WebSocket):
    await ws.accept()
    CLIENTS.add(ws)
    await ws.send_json({"type": "hello", "graph": True})
    try:
        while True:
            await asyncio.sleep(2)
            await ws.send_json({"type": "heartbeat", "trains": list(TRAINS.values())})
    except Exception:
        CLIENTS.remove(ws)
