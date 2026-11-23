import tailwindcss from "@tailwindcss/vite";
import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    // Must match the redirect URI registered for the "eka-web" client in Keycloak.
    port: 5380,
    strictPort: true,
    // Same-origin API calls in development: /api/* is forwarded to the API container.
    // In the Docker image, nginx does the same job (see nginx.conf).
    proxy: {
      "/api": "http://localhost:8300",
    },
  },
  test: {
    environment: "jsdom",
    setupFiles: ["./src/test/setup.ts"],
  },
});
