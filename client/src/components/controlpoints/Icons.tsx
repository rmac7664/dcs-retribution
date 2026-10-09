import { ControlPoint } from "../../api/_liberationApi";
import { Icon, Point } from "leaflet";
import ms from "milsymbol";

/** A thin white edge so the main base's HQ staff shows against other icons. */
const MAIN_BASE_OUTLINE = { outlineWidth: 2, outlineColor: "white" };

export const iconForControlPoint = (cp: ControlPoint) => {
  const options = {
    size: 24,
    colorMode: "Dark",
    ...(cp.main_base ? MAIN_BASE_OUTLINE : {}),
  };
  const symbol = new ms.Symbol(cp.sidc, options);
  // A main base is drawn as a headquarters, with a staff hanging from its box.
  // Anchor it like the plain symbol so the box stays where the base's box would be.
  const anchor = cp.main_base
    ? new ms.Symbol(plainSidc(cp.sidc), options).getAnchor()
    : symbol.getAnchor();

  return new Icon({
    iconUrl: symbol.toDataURL(),
    iconAnchor: new Point(anchor.x, anchor.y),
  });
};

/** The SIDC without the headquarters/task force/dummy indicator (8th digit). */
export const plainSidc = (sidc: string) =>
  sidc.length >= 8 ? sidc.slice(0, 7) + "0" + sidc.slice(8) : sidc;

/** Markers for main bases sit above their neighbours so the base isn't buried. */
export const controlPointZIndex = (cp: ControlPoint) =>
  cp.main_base ? 1500 : 1000;

/** Extra tooltip lines: the enemy's main base, once found, is named as such. */
export const controlPointLines = (cp: ControlPoint): string[] | undefined => {
  if (cp.supply?.lines) {
    return cp.supply.lines;
  }
  return cp.main_base && !cp.blue ? ["Enemy main supply base"] : undefined;
};
