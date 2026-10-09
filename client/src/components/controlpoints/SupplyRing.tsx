import { ControlPoint } from "../../api/_liberationApi";
import { CircleMarker } from "react-leaflet";

/** Ring colour for a base's supply level (the lower of munitions and fuel). */
export function supplyColor(level: number): string {
  if (level >= 66) {
    return "#3fb950";
  }
  if (level >= 33) {
    return "#d29922";
  }
  return "#f85149";
}

interface SupplyRingProps {
  controlPoint: ControlPoint;
}

/** Ring size and line widths, chosen to read on any terrain at any zoom. */
export const RING_RADIUS = 24;
const RING_WEIGHT = 5;
const HALO_WEIGHT = 9;

/**
 * A ring around one of the player's bases showing how well supplied it is:
 * green, amber or red, over a dark halo so it stands out on desert, forest or
 * sea alike, with a light tint inside. Dashed when the base is a supply depot.
 */
export const SupplyRing = (props: SupplyRingProps) => {
  const supply = props.controlPoint.supply;
  if (!supply) {
    return <></>;
  }
  const color = supplyColor(supply.level);
  return (
    <>
      <CircleMarker
        center={props.controlPoint.position}
        radius={RING_RADIUS}
        interactive={false}
        pathOptions={{
          color: "#0d1117",
          opacity: 0.6,
          weight: HALO_WEIGHT,
          fill: false,
        }}
      />
      <CircleMarker
        center={props.controlPoint.position}
        radius={RING_RADIUS}
        interactive={false}
        pathOptions={{
          color: color,
          weight: RING_WEIGHT,
          opacity: 1,
          fill: true,
          fillColor: color,
          fillOpacity: 0.12,
          dashArray: supply.depot ? "10 6" : undefined,
        }}
      />
    </>
  );
};

export default SupplyRing;
