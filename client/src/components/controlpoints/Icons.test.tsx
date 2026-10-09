import { ControlPoint } from "../../api/_liberationApi";
import {
  controlPointLines,
  controlPointZIndex,
  iconForControlPoint,
  plainSidc,
} from "./Icons";
import { LatLng } from "leaflet";

const AIRFIELD = "10032000001213010000";
const AIRFIELD_HQ = "10032002001213010000";

function base(blue: boolean, main_base?: boolean): ControlPoint {
  return {
    id: "foo",
    name: "Ben-Gurion",
    blue: blue,
    position: new LatLng(32, 34.9),
    mobile: false,
    sidc: main_base ? AIRFIELD_HQ : AIRFIELD,
    main_base: main_base,
  };
}

describe("main base marker", () => {
  it("strips the headquarters digit", () => {
    expect(plainSidc(AIRFIELD_HQ)).toBe(AIRFIELD);
  });

  it("keeps the main base's box where a plain base's box would be", () => {
    const plain = iconForControlPoint(base(true)).options.iconAnchor as any;
    const hq = iconForControlPoint(base(true, true)).options.iconAnchor as any;
    // The outline shifts the symbol by its width, nothing more.
    expect(Math.abs(hq.x - plain.x)).toBeLessThan(3);
    expect(Math.abs(hq.y - plain.y)).toBeLessThan(3);
  });

  it("raises main bases above their neighbours", () => {
    expect(controlPointZIndex(base(true, true))).toBeGreaterThan(
      controlPointZIndex(base(true)),
    );
  });

  it("names a found enemy main base in its tooltip", () => {
    expect(controlPointLines(base(false, true))).toEqual([
      "Enemy main supply base",
    ]);
    expect(controlPointLines(base(false))).toBeUndefined();
  });
});
