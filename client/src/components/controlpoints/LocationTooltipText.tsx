import SplitLines from "../splitlines/SplitLines";

interface LocationTooltipTextProps {
  name: string;
  /** Extra lines under the name, such as the base's supply situation. */
  lines?: string[];
}

export const LocationTooltipText = (props: LocationTooltipTextProps) => {
  return (
    <>
      <h3 style={{ margin: 0 }}>{props.name}</h3>
      {props.lines && props.lines.length > 0 && (
        <SplitLines items={props.lines} />
      )}
    </>
  );
};

export default LocationTooltipText;
