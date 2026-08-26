import type { Lane } from "./api.ts";
import { laneClass, laneLabel } from "./format.ts";

/**
 * The coloured lane pill. Teal, amber and coral are the same three names
 * this project has used for the lanes since Phase 4, so the page and the
 * README describe the same thing.
 *
 * A fourth, grey state exists for lane === null. Rows 1-4 were diagnosed
 * before lanes existed; showing them as one of the three would be a
 * quiet lie about what BuildDoctor did that day.
 */
export default function LaneBadge({ lane }: { lane: Lane | null }) {
  return (
    <span
      className={laneClass(lane)}
      title={
        lane === null
          ? "Diagnosed before Phase 4 added lanes"
          : `Lane: ${laneLabel(lane)}`
      }
    >
      {laneLabel(lane)}
    </span>
  );
}
