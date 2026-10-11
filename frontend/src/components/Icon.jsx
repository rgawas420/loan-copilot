// Small stroke icons (24px grid, currentColor).
const PATHS = {
  compose: "M12 20h9M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4Z",
  search: "M11 19a8 8 0 1 0 0-16 8 8 0 0 0 0 16Zm10 2-4.35-4.35",
  navigate: "M3 11 21 3l-8 18-2-8Z",
  pin: "M12 21s7-6 7-11a7 7 0 1 0-14 0c0 5 7 11 7 11Zm0-8a3 3 0 1 0 0-6 3 3 0 0 0 0 6Z",
  sidebar: "M4 4h16a1 1 0 0 1 1 1v14a1 1 0 0 1-1 1H4a1 1 0 0 1-1-1V5a1 1 0 0 1 1-1Zm5 0v16",
  menu: "M4 7h16M4 12h16M4 17h16",
  send: "M12 19V5m0 0-6 6m6-6 6 6",
  more: "M5 12h.01M12 12h.01M19 12h.01",
  trash: "M4 7h16M10 11v6m4-6v6M6 7l1 13h10l1-13M9 7V4h6v3",
  edit: "M4 20h4L19 9l-4-4L4 16v4Zm9-13 4 4",
  download: "M12 4v11m0 0-5-5m5 5 5-5M5 20h14",
  logout: "M15 4h4a1 1 0 0 1 1 1v14a1 1 0 0 1-1 1h-4M10 17l5-5-5-5m5 5H3",
  shield: "M12 3 4 6v6c0 5 3.5 8 8 9 4.5-1 8-4 8-9V6Z",
  sheet: "M9 4h6v3H9zM7 5H5v16h14V5h-2M8 11h8M8 15h8M8 19h5",
  copy: "M9 9h11v11H9zM5 15H4V4h11v1",
  check: "m5 12 5 5L20 7",
  x: "M6 6l12 12M18 6 6 18",
  key: "M15 7a4 4 0 1 1-3.9 4.9L4 19v2h3v-2h2v-2h2l1.1-1.1A4 4 0 0 1 15 7Z",
  back: "M15 18l-6-6 6-6",
  spark: "M12 3v4m0 10v4M3 12h4m10 0h4M6 6l2.5 2.5m7 7L18 18M6 18l2.5-2.5m7-7L18 6",
  book: "M4 5a2 2 0 0 1 2-2h14v16H6a2 2 0 0 0-2 2V5Zm0 14a2 2 0 0 1 2-2h14",
  card: "M3 6h18v12H3zM3 10h18M7 15h4",
  gauge: "M4 20V10m6 10V4m6 16v-7m4 7H2",
  chevron: "m6 9 6 6 6-6",
  users: "M16 19v-1a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v1M9 10a3 3 0 1 0 0-6 3 3 0 0 0 0 6Zm13 9v-1a4 4 0 0 0-3-3.9M16 4.1a3 3 0 0 1 0 5.8",
  pulse: "M3 12h4l3-8 4 16 3-8h4",
  folder: "M3 6a1 1 0 0 1 1-1h5l2 2h9a1 1 0 0 1 1 1v10a1 1 0 0 1-1 1H4a1 1 0 0 1-1-1Z",
  plus: "M12 5v14M5 12h14",
  up: "M7 10v10H4V10Zm0 0 4-7a2 2 0 0 1 3 2l-1 4h6a2 2 0 0 1 2 2.3l-1.3 7A2 2 0 0 1 17.7 20H7",
  down: "M17 14V4h3v10Zm0 0-4 7a2 2 0 0 1-3-2l1-4H5a2 2 0 0 1-2-2.3l1.3-7A2 2 0 0 1 6.3 4H17",
  refresh: "M20 11a8 8 0 1 0-2.3 5.7M20 4v7h-7",
  arrowDown: "M12 5v14m0 0-6-6m6 6 6-6",
  pin: "M9 4h6l-1 5 4 4H6l4-4ZM12 13v8",
  archive: "M3 4h18v4H3zM5 8v12h14V8M10 12h4",
  alert: "M12 3 2 20h20ZM12 10v4m0 3h.01",
  file: "M6 3h8l4 4v14H6ZM14 3v4h4M9 12h6M9 16h6",
  image: "M4 5h16v14H4ZM4 16l5-5 4 4 3-3 4 4M15.5 9.5h.01",
  play: "M7 5v14l11-7Z",
};

export default function Icon({ name, size = 18, className = "" }) {
  return (
    <svg className={`icon ${className}`} width={size} height={size} viewBox="0 0 24 24" fill="none"
         stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d={PATHS[name]} />
    </svg>
  );
}
