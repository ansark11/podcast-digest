import { defineCollection } from 'astro:content';
import { glob } from 'astro/loaders';
import { z } from 'astro/zod';

// Reads the pipeline's committed output directly: ../summaries/<show>/<file>.json
// (one file per episode). The schema mirrors build_summary_record() in
// scripts/podcast_digest.py; a file that doesn't match fails the build loudly.
const summaries = defineCollection({
  loader: glob({
    pattern: '*/*.json',
    base: '../summaries',
    generateId: ({ entry }) => entry.split('/').pop()!.replace(/\.json$/, ''),
  }),
  schema: z.object({
    schema_version: z.number(),
    guid: z.string(),
    show: z.string(),
    show_slug: z.string(),
    episode_title: z.string(),
    published: z.string().nullable(),
    audio_url: z.string().nullable().optional(),
    episode_url: z.string().nullable().optional(),
    artwork_url: z.string().nullable().optional(),
    description_html: z.string().nullable().optional(),
    duration_seconds: z.number().nullable().optional(),
    transcript_path: z.string().nullable().optional(),
    transcription_source: z.string().nullable().optional(),
    summary_model: z.string(),
    prompt_version: z.string(),
    generated_at: z.string(),
    summary: z.object({
      overview: z.string(),
      takeaways: z.array(z.object({ heading: z.string().nullable(), detail: z.string() })),
      quotes: z.array(z.object({ text: z.string(), speaker: z.string().nullable() })),
      resources: z.array(z.object({ name: z.string(), kind: z.string() })),
    }),
  }),
});

export const collections = { summaries };
