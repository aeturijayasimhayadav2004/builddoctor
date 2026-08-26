import type { Diagnosis } from "./api.ts";
import { EMPTY, percent } from "./format.ts";

/**
 * Whether BuildDoctor's memory was involved, in three states.
 *
 * The three are genuinely different and the table should not blur them:
 *
 *   matched      memory found a past failure above 0.90 and the model was
 *                given it as a hint.
 *   no match     memory ran and deliberately returned nothing, because
 *                the closest row was not close enough. This is the
 *                threshold working, not a failure.
 *   dash         memory did not exist when this row was written.
 */
export default function MemoryCell({ row }: { row: Diagnosis }) {
  if (row.memory_match) {
    return (
      <span
        className="chip chip-memory"
        title={`Matched diagnosis #${row.memory_match.row_id} at ${percent(
          row.memory_match.similarity,
        )} similarity`}
      >
        #{row.memory_match.row_id} · {percent(row.memory_match.similarity)}
      </span>
    );
  }

  if (row.memory_checked) {
    return (
      <span className="chip chip-quiet" title="Memory ran, nothing was above the 0.90 threshold">
        no match
      </span>
    );
  }

  return (
    <span className="muted" title="Written before Phase 6 added memory">
      {EMPTY}
    </span>
  );
}
