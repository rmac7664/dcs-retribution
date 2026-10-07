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

/**
 * A ring around one of the player's bases showing how well supplied it is:
 * green, amber or red. Dashed when the base is a supply depot.
 */
export const SupplyRing = (props: SupplyRingProps) => {
  const supply = props.controlPoint.supply;
  if (!supply) {
    return <></>;
  }
  return (
    <CircleMarker
      center={props.controlPoint.position}
      radius={20}
      interactive={false}
      pathOptions={{
        color: supplyColor(supply.level),
        weight: 3,
        fill: false,
        dashArray: supply.depot ? "6 4" : undefined,
      }}
    />
  );
};

export default SupplyRing;
