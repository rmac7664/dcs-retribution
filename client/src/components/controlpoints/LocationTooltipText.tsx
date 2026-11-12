import SplitLines from "../splitlines/SplitLines";

interface LocationTooltipTextProps {
  name: string;
  /** Extra lines under the name, such as the base's supply situation. */
  lines?: string[];
  units?: string[];
  tacan?: string | null;
  atcFrequency?: string | null;
}

export const LocationTooltipText = (props: LocationTooltipTextProps) => {
  return (
    <>
      <h3 style={{ margin: 0 }}>{props.name}</h3>
      {props.lines && props.lines.length > 0 && (
        <SplitLines items={props.lines} />
      )}
      {props.atcFrequency && (
        <div style={{ marginTop: 2 }}>ATC: {props.atcFrequency}</div>
      )}
      {props.tacan && <div>TACAN: {props.tacan}</div>}
      {props.units && props.units.length > 0 && (
        <SplitLines items={props.units} />
      )}
    </>
  );
};

export default LocationTooltipText;
