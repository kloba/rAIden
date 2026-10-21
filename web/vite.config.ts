import tailwindcss from "@tailwindcss/vite";
import react from "@vitejs/plugin-react";
import { fileURLToPath, URL } from "node:url";
import { defineConfig } from "vite";

// `npm run dev` proxies the API to a console started with `raiden console --demo --port 8787`
export default defineConfig({
  plugins: [react(), tailwindcss()],
  resolve: { alias: { "@": fileURLToPath(new URL("./src", import.meta.url)) } },
  server: { port: 5173, proxy: { "/api": { target: "http://127.0.0.1:8787", changeOrigin: false } } },
  build: {
    outDir: "dist",
    sourcemap: false,
    rolldownOptions: {
      output: {
        // vendor code changes less often than the app: separate chunks cache across releases
        advancedChunks: {
          groups: [
            { name: "charts", test: /node_modules[\\/](recharts|d3-[a-z-]+|victory-vendor|decimal\.js-light|internmap|eventemitter3|@reduxjs|reselect|immer|redux)/ },
            { name: "markdown", test: /node_modules[\\/](react-markdown|remark-[a-z-]+|micromark[a-z-]*|mdast-[a-z-]+|unified|hast-[a-z-]+|unist-[a-z-]+|vfile[a-z-]*|property-information|space-separated-tokens|comma-separated-tokens|decode-named-character-reference|character-entities[a-z-]*|trim-lines|bail|devlop|ccount|markdown-table|zwitch|longest-streak|html-url-attributes|estree-util-[a-z-]+|is-plain-obj|trough|extend)[\\/]/ },
            { name: "vendor", test: /node_modules[\\/](?!@fontsource)/ },
          ],
        },
      },
    },
  },
});
