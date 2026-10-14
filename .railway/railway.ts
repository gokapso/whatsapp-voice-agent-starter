// Railway project for this starter, as Infrastructure as Code (https://docs.railway.com/infrastructure-as-code).
// `railway config plan` previews it and `railway config apply` creates or updates the linked project.
// Full guide, including the one-click template made from this project: docs/deploy-railway.md.
//
// One service built from the repository's Dockerfile, one 1 GB volume for DATA_DIR, one replica
// (call state is in memory and a volume cannot be shared by replicas). Railway has no inbound
// UDP, so call audio needs TURN: set TURN_PROVIDER=cloudflare and its two keys.
//
// No secret is written here. preserve() keeps each value already set on the service, with
// `voice-agent railway variables --yes` or the dashboard. Set them before the first apply.
import { defineRailway, github, preserve, project, service, volume } from "railway/iac";

export default defineRailway(() => {
  const data = volume("voice-agent-data", { sizeMB: 1024 });
  const agent = service("voice-agent", {
    // Fork the repository to deploy your own business.json and prompt, and point this at the fork.
    source: github("gokapso/whatsapp-voice-agent-starter", { branch: "main" }),
    build: { builder: "DOCKERFILE", dockerfilePath: "Dockerfile" },
    healthcheck: "/healthz",
    healthcheckTimeout: 120,
    replicas: 1,
    deploy: { restartPolicyType: "ON_FAILURE", restartPolicyMaxRetries: 5 },
    volumeMounts: { "/app/data": data },
    env: {
      // Railway mounts volumes as root. The image starts as root only to give DATA_DIR to its
      // unprivileged user, then drops root before the server starts (docker-entrypoint.sh).
      RAILWAY_RUN_UID: "0",
      ALLOWED_HOSTS: "${{RAILWAY_PUBLIC_DOMAIN}}",
      VOICE_PROVIDER: preserve(),
      XAI_API_KEY: preserve(),
      ELEVENLABS_API_KEY: preserve(),
      ELEVENLABS_AGENT_ID: preserve(),
      CAL_API_KEY: preserve(),
      CALENDAR: preserve(),
      OPERATOR_TOKEN: preserve(),
      CALLER_KEY_SECRET: preserve(),
      KAPSO_API_KEY: preserve(),
      WHATSAPP_PHONE_NUMBER_ID: preserve(),
      WHATSAPP_WEBHOOK_SECRET: preserve(),
      TURN_PROVIDER: preserve(),
      CLOUDFLARE_TURN_KEY_ID: preserve(),
      CLOUDFLARE_TURN_API_TOKEN: preserve(),
    },
  });
  return project("whatsapp-voice-agent", { resources: [agent, data] });
});
