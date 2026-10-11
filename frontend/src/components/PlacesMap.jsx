import { useLayoutEffect, useMemo, useRef, useState } from "react";
import Icon from "./Icon.jsx";

// Under a customer-profile answer, like ChatGPT's map: the lenders' branches in the customer's city on an
// OpenStreetMap preview (numbered pins), then place cards. A card opens directions in Google Maps; a lender
// with no branch on OpenStreetMap gets a "Search in Maps" card. The map is plain tile images: no map library.

const TILE = 256;
const HEIGHT = 220;

function project(lat, lon, z) {
  const size = TILE * 2 ** z;
  const r = (Math.max(Math.min(lat, 85), -85) * Math.PI) / 180;
  return [((lon + 180) / 360) * size, ((1 - Math.log(Math.tan(r) + 1 / Math.cos(r)) / Math.PI) / 2) * size];
}

/** The zoom that fits every pin, the tiles covering the view and the pins' positions (same as the mobile app). */
export function mapLayout(points, width, height, pad = 36, maxZoom = 15) {
  if (!points.length || width <= 0) return { zoom: 0, tiles: [], pins: [] };
  let zoom = points.length === 1 ? Math.min(maxZoom, 14) : maxZoom;
  const span = (z) => {
    const ps = points.map(([la, lo]) => project(la, lo, z));
    const xs = ps.map((p) => p[0]), ys = ps.map((p) => p[1]);
    return { ps, xs, ys };
  };
  for (; zoom > 3; zoom--) {
    const { xs, ys } = span(zoom);
    if (Math.max(...xs) - Math.min(...xs) <= width - 2 * pad && Math.max(...ys) - Math.min(...ys) <= height - 2 * pad) break;
  }
  const { ps, xs, ys } = span(zoom);
  const left = (Math.min(...xs) + Math.max(...xs)) / 2 - width / 2;
  const top = (Math.min(...ys) + Math.max(...ys)) / 2 - height / 2;
  const n = 2 ** zoom, tiles = [];
  for (let ty = Math.floor(top / TILE); ty <= Math.floor((top + height) / TILE); ty++) {
    if (ty < 0 || ty >= n) continue;
    for (let tx = Math.floor(left / TILE); tx <= Math.floor((left + width) / TILE); tx++) {
      const x = ((tx % n) + n) % n;
      tiles.push({ key: `${zoom}/${tx}/${ty}`, url: `https://tile.openstreetmap.org/${zoom}/${x}/${ty}.png`,
                   left: tx * TILE - left, top: ty * TILE - top });
    }
  }
  return { zoom, tiles, pins: ps.map((p) => ({ left: p[0] - left, top: p[1] - top })) };
}

export default function PlacesMap({ places }) {
  const box = useRef(null);
  const [width, setWidth] = useState(0);
  useLayoutEffect(() => {
    const el = box.current;
    if (!el) return undefined;
    const ro = new ResizeObserver(([e]) => setWidth(Math.round(e.contentRect.width)));
    ro.observe(el);
    return () => ro.disconnect();
  }, []);
  const layout = useMemo(() => mapLayout(places.places.map((p) => [p.lat, p.lon]), width, HEIGHT), [places.places, width]);
  const [lat, lon] = places.center;
  const mapUrl = `https://www.openstreetmap.org/#map=${Math.max(layout.zoom, 11)}/${lat.toFixed(4)}/${lon.toFixed(4)}`;
  return (
    <section className="places" aria-label={`Lender branches in ${places.city}`}>
      {places.places.length > 0 && (
        <a ref={box} className="places-map" href={mapUrl} target="_blank" rel="noopener noreferrer"
           aria-label={`Open the map of ${places.city}`} style={{ height: HEIGHT }}>
          {layout.tiles.map((t) => (
            <img key={t.key} src={t.url} alt="" loading="lazy" draggable="false" style={{ left: t.left, top: t.top }} />
          ))}
          {layout.pins.map((p, i) => (
            <span key={i} className="places-pin" style={{ left: p.left, top: p.top }} title={places.places[i].name}>{i + 1}</span>
          ))}
          <small className="places-attr">{places.attribution}</small>
        </a>
      )}
      <div className="places-cards">
        {places.places.map((p, i) => (
          <a key={`${p.lender}-${i}`} className="place-card" href={p.directions} target="_blank" rel="noopener noreferrer"
             title={`Directions to ${p.name}`}>
            <span className="place-head"><span className="place-num">{i + 1}</span><b>{p.name}</b></span>
            {p.address && <span className="place-line">{p.address}</span>}
            <span className="place-line">{p.km} km from the centre{p.hours ? ` · ${p.hours}` : ""}</span>
            <span className="place-go"><Icon name="navigate" size={13} />Directions</span>
          </a>
        ))}
        {places.missing.map((m) => (
          <a key={m.lender} className="place-card" href={m.search} target="_blank" rel="noopener noreferrer"
             title={`Search ${m.lender} in Maps`}>
            <span className="place-head"><b>{m.lender}</b></span>
            <span className="place-line">Branch not on OpenStreetMap yet</span>
            <span className="place-go"><Icon name="search" size={13} />Search in Maps</span>
          </a>
        ))}
      </div>
    </section>
  );
}
