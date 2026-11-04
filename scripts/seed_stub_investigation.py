"""Seed one stub-provider investigation into the DEV database so the Agent
Console trace UI can be verified end-to-end.

The session's model label carries STUB-PROVIDER-TEST-ONLY and the UI displays
it verbatim, so this can never be mistaken for real inference. Development
verification only.
"""
import os
import sys

os.environ["DATABASE_URL"] = "sqlite:///backend/data/routeshield_dev.db"
os.environ["AUTO_REFRESH_RECALL"] = "false"
sys.path.insert(0, "backend")

from app import models  # noqa: E402
from app.db import SessionLocal, Base, engine  # noqa: E402
from app.seed import seed_all  # noqa: E402
from app.services import agent as agent_svc  # noqa: E402
import tests.test_agent as T  # noqa: E402

Base.metadata.drop_all(engine)
Base.metadata.create_all(engine)
db = SessionLocal()
seed_all(db)
incident = db.query(models.Incident).order_by(models.Incident.id).first()

# One full happy-path investigation + one honest failure, both stub-labelled.
ok = agent_svc.run_investigation(
    db, incident_id=incident.id,
    objective="Assess recall exposure for every vehicle and prepare recovery options for disrupted routes.",
    requested_by="fleet.manager",
    provider=agent_svc.StubChatProvider(T._happy_script(db, incident)),
)
fail = agent_svc.run_investigation(
    db, incident_id=incident.id,
    objective="Which vehicles can be verified, and what evidence is missing for the rest?",
    requested_by="fleet.manager",
    provider=agent_svc.VultrChatProvider(),
)
print("OK session:", ok.session_uid, ok.status, "| FAIL session:", fail.session_uid, fail.stop_reason)
db.close()
