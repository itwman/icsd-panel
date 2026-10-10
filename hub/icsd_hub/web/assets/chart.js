// A thin wrapper over uPlot (loaded as a classic script: window.uPlot).
// uPlot draws ~100k points on a canvas without breaking a sweat and is
// 50 KB — the right tool for a panel that may run on a small VPS.
import { html, useEffect, useRef } from "./vendor/preact-htm.js";
import { dayOnly, fa, timeOnly } from "./lib.js";

const cssVar = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();

export function Chart({ data, series, fmt = (v) => fa(v), yMax, height = 200, theme }) {
  const box = useRef(null);
  const plot = useRef(null);

  const toArrays = () => [data.t, ...series.map((s) => data[s.key] || [])];

  useEffect(() => {
    if (!box.current || !data || !window.uPlot) return;
    const text = cssVar("--muted"), grid = cssVar("--line");
    const span = data.t.length ? data.t[data.t.length - 1] - data.t[0] : 0;
    const axis = { stroke: text, grid: { stroke: grid, width: 1 }, ticks: { stroke: grid, width: 1 },
      font: '11px Vazirmatn, system-ui, sans-serif' };
    const opts = {
      width: box.current.clientWidth || 400,
      height,
      padding: [8, 6, 0, 0],
      cursor: { drag: { x: false, y: false }, points: { size: 6 } },
      legend: { live: true },
      scales: { x: { time: true }, y: { range: (u, min, max) => [0, Math.max(yMax || 0, max * 1.1 || 1)] } },
      axes: [
        { ...axis, values: (u, ticks) => ticks.map((t) => (span > 2 * 86400 ? dayOnly(t) : timeOnly(t))) },
        { ...axis, size: 62, values: (u, ticks) => ticks.map((v) => fmt(v)) },
      ],
      series: [
        { label: "زمان", value: (u, v) => (v == null ? "—" : (span > 2 * 86400 ? dayOnly(v) + " " : "") + timeOnly(v)) },
        ...series.map((s) => ({
          label: s.label,
          stroke: cssVar(s.color),
          width: 1.6,
          fill: s.fill ? cssVar(s.color) + "22" : undefined,
          points: { show: false },
          value: (u, v) => (v == null ? "—" : fmt(v)),
        })),
      ],
    };
    plot.current = new window.uPlot(opts, toArrays(), box.current);
    const ro = new ResizeObserver(() => {
      if (plot.current && box.current) plot.current.setSize({ width: box.current.clientWidth, height });
    });
    ro.observe(box.current);
    return () => { ro.disconnect(); plot.current && plot.current.destroy(); plot.current = null; };
  }, [series.map((s) => s.key).join(), theme, height, data && data.step]);

  useEffect(() => {
    if (plot.current && data) plot.current.setData(toArrays());
  }, [data]);

  return html`<div class="chart" ref=${box}></div>`;
}
