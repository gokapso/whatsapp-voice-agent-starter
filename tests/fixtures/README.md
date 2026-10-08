# Synthetic fixtures

Every value here is made up: 555-01xx phone numbers (reserved for fiction), placeholder
`wacid.SYNTHETIC-*` call IDs, `US.1000...` user IDs, digit-only fake media IDs and a `.invalid`
media URL. Shapes follow Kapso's public Calling docs (Meta payload structure forwarded through a
`kind: "meta"` webhook). They are not captured traffic. Tests replace `session.sdp` with a real SDP
from a local aiortc peer when they need media.
