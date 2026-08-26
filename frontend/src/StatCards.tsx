import type { Lane, Stats } from "./api.ts";
import { laneLabel, relativeTime } from "./format.ts";
import { Database, Layers, Pulse, Sparkle } from "./icons.tsx";

/**
 * The numbers across the top.
 *
 * The memory card deliberately shows "1 of 2" as well as the percentage.
 * A bare "50%" invites the reader to assume it is 50% of everything on the
 * page, and it is not - eleven of these thirteen rows were written before
 * memory existed and were never asked. The denominator is the honest part.
 */

/** null is a real lane value here - rows 1-4 predate lanes. */
export type LaneFilter = Lane | null | undefined;

interface Props {
  stats: Stats;
  /** undefined means no filter; null means the "unclassified" bucket. */
  filter: LaneFilter;
  onFilter: (lane: LaneFilter) => void;
}

export default function StatCards({ stats, filter, onFilter }: Props) {
  const { memory } = stats;
  const filtering = filter !== undefined;

  return (
    <section className="cards">
      <article className="card enter" style={{ animationDelay: "0ms" }}>
        <div className="card-head">
          <span className="card-icon tint-teal">
            <Pulse size={15} />
          </span>
          <h2>Diagnoses</h2>
        </div>
        <p className="figure">{stats.total}</p>
        <p className="note">
          across {stats.repos} {stats.repos === 1 ? "repository" : "repositories"}
          {stats.latest_at ? `, latest ${relativeTime(stats.latest_at)}` : ""}
        </p>
      </article>

      <article className="card card-wide enter" style={{ animationDelay: "45ms" }}>
        <div className="card-head">
          <span className="card-icon">
            <Layers size={15} />
          </span>
          <h2>By lane</h2>
        </div>

        <div
          className={filtering ? "lane-bar has-filter" : "lane-bar"}
          role="img"
          aria-label="Share of diagnoses per lane"
        >
          {stats.lanes
            .filter((entry) => entry.count > 0)
            .map((entry) => (
              <div
                key={entry.lane ?? "none"}
                className={
                  `lane-slice lane-${entry.lane ?? "none"}` +
                  (filtering && entry.lane === filter ? " is-active" : "")
                }
                style={{ width: `${entry.percent}%` }}
                title={`${laneLabel(entry.lane)}: ${entry.count} (${entry.percent}%)`}
              />
            ))}
        </div>

        {/* Each legend row filters the table below.
         *
         * Real <button> elements rather than divs with onClick. That gets
         * Tab focus, Enter and Space, and a screen-reader announcement for
         * free, and aria-pressed is the standard way to say "this toggle is
         * currently on" - none of which a div can claim without
         * reimplementing all of it by hand and getting some of it wrong.
         *
         * Clicking the active lane again clears the filter, so the control
         * that turned something on is also the one that turns it off. */}
        <ul className="lane-legend">
          {stats.lanes.map((entry) => {
            const active = filter === entry.lane && filtering;
            const empty = entry.count === 0;
            return (
              <li key={entry.lane ?? "none"}>
                <button
                  type="button"
                  aria-pressed={active}
                  disabled={empty}
                  onClick={() => onFilter(active ? undefined : entry.lane)}
                  title={
                    empty
                      ? `No diagnoses in the ${laneLabel(entry.lane)} lane`
                      : active
                        ? "Show every lane again"
                        : `Show only ${laneLabel(entry.lane)}`
                  }
                >
                  <span
                    className={`dot lane-${entry.lane ?? "none"}`}
                    aria-hidden="true"
                  />
                  {laneLabel(entry.lane)}
                  <span className="count">{entry.percent}%</span>
                  <span className="of">{entry.count}</span>
                </button>
              </li>
            );
          })}
        </ul>
      </article>

      <article className="card enter" style={{ animationDelay: "90ms" }}>
        <div className="card-head">
          <span className="card-icon tint-violet">
            <Sparkle size={15} />
          </span>
          <h2>Memory hits</h2>
        </div>
        <p className="figure">
          {memory.rate}
          <span className="unit">%</span>
        </p>
        <p className="note">
          {memory.hits} of {memory.asked}{" "}
          {memory.asked === 1 ? "failure" : "failures"} since memory existed
        </p>
      </article>

      <article className="card enter" style={{ animationDelay: "135ms" }}>
        <div className="card-head">
          <span className="card-icon">
            <Database size={15} />
          </span>
          <h2>Searchable</h2>
        </div>
        <p className="figure">
          {memory.searchable}
          <span className="unit">/{stats.total}</span>
        </p>
        <p className="note">rows with an embedding, findable by a future failure</p>
      </article>
    </section>
  );
}
