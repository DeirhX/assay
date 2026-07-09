import type { ActivityEvent, JobListing } from "./api-types";

// Pure helpers for the Activity view, kept dependency-free so they can be unit
// tested without dragging in the app shell (activity.ts imports shell/tasks for
// its DOM + routing side of the world).

// A finished task stores only flat routing ids in the feed; rebuild the minimal
// JobListing shape navForTask/taskTitle expect -- they read nested .artifact.stem
// and .result.slug, which the feed flattens to artifact_stem / slug.
export function asJob(ev: ActivityEvent): JobListing {
  return {
    id: ev.id || "",
    kind: ev.kind || "",
    state: (ev.state || "done") as JobListing["state"],
    message: ev.message || "",
    symbol: ev.symbol,
    segment: ev.segment,
    run_id: ev.run_id,
    stem: ev.stem,
    artifact: ev.artifact_stem ? { stem: ev.artifact_stem } : null,
    result: ev.slug ? { slug: ev.slug } : null,
    error: ev.error,
    cancelled: ev.state === "cancelled",
    created_at: ev.ts,
    updated_at: ev.ts,
  };
}

// Day bucket label for the group headers: Today / Yesterday / N days ago / date.
export function dayLabel(iso: string, now: Date = new Date()): string {
  const t = Date.parse(iso);
  if (!t) return "Earlier";
  const d = new Date(t);
  const startOf = (x: Date) => new Date(x.getFullYear(), x.getMonth(), x.getDate()).getTime();
  const days = Math.round((startOf(now) - startOf(d)) / 86400000);
  if (days <= 0) return "Today";
  if (days === 1) return "Yesterday";
  if (days < 7) return `${days} days ago`;
  return d.toLocaleDateString();
}
