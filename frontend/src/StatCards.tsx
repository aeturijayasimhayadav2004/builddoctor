import type { Stats } from "./api.ts";
import { laneLabel, relativeTime } from "./format.ts";

/**
 * The numbers across the top.
 *
 * The memory card deliberately shows "1 of 2" as well as the percentage.
 * A bare "50%" invites the reader to assume it is 50% of everything on the
 * page, and it is not - eleven of these thirteen rows were written before
 * memory existed and were never asked. The denominator is the honest part.
 */
export default function StatCards({ stats }: { stats: Stats }) {
  const { memory } = stats;

  return (
    <section className="cards">
      <article className="card">
        <h2>Diagnoses</h2>
        <p className="figure">{stats.total}</p>
        <p className="note">
          across {stats.repos} {stats.repos === 1 ? "repository" : "repositories"}
          {stats.latest_at ? `, latest ${relativeTime(stats.latest_at)}` : ""}
        </p>
      </article>

      <article className="card card-wide">
        <h2>By lane</h2>
        <div className="lane-bar" role="img" aria-label="Share of diagnoses per lane">
          {stats.lanes
            .filter((entry) => entry.count > 0)
            .map((entry) => (
              <div
                key={entry.lane ?? "none"}
                className={`lane-slice lane-${entry.lane ?? "none"}`}
                style={{ width: `${entry.percent}%` }}
                title={`${laneLabel(entry.lane)}: ${entry.count} (${entry.percent}%)`}
              />
            ))}
        </div>
        <ul className="lane-legend">
          {stats.lanes.map((entry) => (
            <li key={entry.lane ?? "none"}>
              <span className={`dot lane-${entry.lane ?? "none"}`} />
              {laneLabel(entry.lane)}
              <strong>{entry.percent}%</strong>
              <span className="muted">({entry.count})</span>
            </li>
          ))}
        </ul>
      </article>

      <article className="card">
        <h2>Memory hits</h2>
        <p className="figure">
          {memory.rate}
          <span className="unit">%</span>
        </p>
        <p className="note">
          {memory.hits} of {memory.asked}{" "}
          {memory.asked === 1 ? "failure" : "failures"} since memory existed
        </p>
      </article>

      <article className="card">
        <h2>Searchable</h2>
        <p className="figure">
          {memory.searchable}
          <span className="unit">/{stats.total}</span>
        </p>
        <p className="note">rows with an embedding, findable by a future failure</p>
      </article>
    </section>
  );
}
