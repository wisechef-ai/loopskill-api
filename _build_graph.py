import json
from pathlib import Path
from graphify.build import build_from_json
from graphify.cluster import cluster, score_all
from graphify.analyze import god_nodes, surprising_connections, suggest_questions
from graphify.report import generate
from graphify.export import to_html, to_json

REPO = Path('/home/adam/repos/loopskill-api')
ast = json.loads((REPO / '.graphify_ast.json').read_text())

# Hand-crafted semantic layer: the evergreen GitOps control plane architecture
sem_nodes = [
 {"id":"loopskill","type":"project","label":"LoopSkill","description":"Open-core fleet control plane + skill/bundle/loop/personality registry (evolution of Recipes)","source":"README","provenance":"EXTRACTED"},
 {"id":"cp_evergreen","type":"concept","label":"Evergreen GitOps Control Plane","description":"desired-state per fleet -> server diff engine -> conditional reconcile endpoint -> atomic client w/ rollback -> drift observability","source":"reconcile_client.py","provenance":"EXTRACTED"},
 {"id":"recon_engine","type":"process","label":"Reconcile Engine (server, Phase B)","description":"app/services/reconcile.py computes add/update/remove/drift diff vs lockfile generation","source":"services/reconcile.py","provenance":"EXTRACTED"},
 {"id":"recon_client","type":"process","label":"Atomic Reconcile Client (host)","description":"ReconcileClient: snapshot LKG -> verify sha256 -> os.replace swap -> health check -> auto-rollback","source":"reconcile_client.py","provenance":"EXTRACTED"},
 {"id":"recon_cli","type":"artifact","label":"recipes-reconcile CLI (Phase J)","description":"host cron entrypoint; POST /api/reconcile If-None-Match; ships AS a skill so it self-updates","source":"reconcile_cli.py","provenance":"EXTRACTED"},
 {"id":"fleet","type":"concept","label":"Fleet + Subscription + channels","description":"Fleet/FleetSubscription models; stable/canary/frozen channels; cross-fleet isolation tested","source":"models.py","provenance":"EXTRACTED"},
 {"id":"bundle_deploy","type":"process","label":"Bundle Deployment","description":"bundle_deployment_routes.py (425 LOC) + BundleDeployment model — deploy a bundle to a fleet","source":"bundle_deployment_routes.py","provenance":"EXTRACTED"},
 {"id":"drift_obs","type":"concept","label":"Drift Observability (Phase I)","description":"ReconcileEvent + FleetPing + heartbeat — desired-vs-actual visibility","source":"models.py","provenance":"EXTRACTED"},
 {"id":"artifacts4","type":"concept","label":"4 deployable artifact types","description":"Skill, Bundle, Loop, Personality (+versions) — the deployable catalog","source":"models.py","provenance":"EXTRACTED"},
 {"id":"mcp","type":"technology","label":"MCP server (loopskill_* tools)","description":"StreamableHTTP+stdio; recipes_*->loopskill_* renamed w/ aliases","source":"mcp/server.py","provenance":"EXTRACTED"},
 {"id":"gap_state","type":"constraint","label":"BUILT BUT COLD","description":"live app.loopskill.io = v0.5.0 seed fixtures, 0 real installs, 0 fleets operated — control plane never run for real","source":"live-probe","provenance":"EXTRACTED"},
 {"id":"gap_mcpcfg","type":"constraint","label":"GAP: MCP-config not a deployable artifact","description":"zai_websearch etc must be hand-wired per profile; no 5th artifact type for MCP/CLI config","source":"fleet-inventory","provenance":"INFERRED","confidence":0.8},
 {"id":"gap_resid","type":"constraint","label":"GAP: no residency-eligibility flag","description":"Praga NIS2+RODO+REMIT-gated; zai_websearch China-hosted; bundle needs residency gate before deploy","source":"fleet-inventory","provenance":"INFERRED","confidence":0.85},
]
def nid(name):
    for n in ast['nodes']:
        if (n.get('label') or n.get('name'))==name: return n.get('id')
    return None
sem_edges = [
 {"source":"loopskill","target":"cp_evergreen","relation":"is_part_of","provenance":"EXTRACTED"},
 {"source":"cp_evergreen","target":"recon_engine","relation":"uses","provenance":"EXTRACTED"},
 {"source":"cp_evergreen","target":"recon_client","relation":"uses","provenance":"EXTRACTED"},
 {"source":"recon_cli","target":"recon_client","relation":"uses","provenance":"EXTRACTED"},
 {"source":"recon_cli","target":"recon_engine","relation":"depends_on","provenance":"EXTRACTED"},
 {"source":"cp_evergreen","target":"fleet","relation":"uses","provenance":"EXTRACTED"},
 {"source":"bundle_deploy","target":"fleet","relation":"relates_to","provenance":"EXTRACTED"},
 {"source":"cp_evergreen","target":"drift_obs","relation":"uses","provenance":"EXTRACTED"},
 {"source":"loopskill","target":"artifacts4","relation":"uses","provenance":"EXTRACTED"},
 {"source":"loopskill","target":"mcp","relation":"uses","provenance":"EXTRACTED"},
 {"source":"loopskill","target":"gap_state","relation":"relates_to","provenance":"EXTRACTED"},
 {"source":"artifacts4","target":"gap_mcpcfg","relation":"relates_to","provenance":"INFERRED"},
 {"source":"bundle_deploy","target":"gap_resid","relation":"relates_to","provenance":"INFERRED"},
]
for a,b in [("recon_engine","Skill"),("recon_engine","Bundle"),("fleet","Fleet"),("fleet","FleetSubscription"),("bundle_deploy","BundleDeployment"),("drift_obs","ReconcileEvent"),("drift_obs","FleetPing"),("artifacts4","Loop"),("artifacts4","Personality")]:
    t=nid(b)
    if t: sem_edges.append({"source":a,"target":t,"relation":"relates_to","provenance":"INFERRED","confidence":0.7})

merged={"nodes":ast['nodes']+sem_nodes,"edges":ast['edges']+sem_edges,"input_tokens":0,"output_tokens":0}
G=build_from_json(merged)
comms=cluster(G)
coh=score_all(G,comms)
labels={}
for cid,mem in comms.items():
    ls=[G.nodes[m].get('label',m) for m in mem if G.has_node(m)]
    labels[cid]=ls[0] if ls else f"C{cid}"
gods=god_nodes(G); surp=surprising_connections(G); qs=suggest_questions(G,comms,labels)
OUT=REPO/'graphify-out'; OUT.mkdir(exist_ok=True)
to_json(G,comms,str(OUT/'graph.json')); to_html(G,comms,str(OUT/'graph.html'))
det={"files":{"code":189,"document":0},"total_files":189,"total_words":154639}
rep=generate(G,comms,coh,labels,gods,surp,det,{"input_tokens":0,"output_tokens":0},str(REPO),qs)
(OUT/'GRAPH_REPORT.md').write_text(rep)
print("nodes",G.number_of_nodes(),"edges",G.number_of_edges(),"communities",len(comms))
print("report:",OUT/'GRAPH_REPORT.md')
