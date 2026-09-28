// @ts-check
import { defineConfig } from 'astro/config';

// Static site: every summary is pre-built as its own page at build time.
// Vercel rebuilds whenever a commit lands (including the pipeline's daily one).
export default defineConfig({
  trailingSlash: 'ignore',
});
