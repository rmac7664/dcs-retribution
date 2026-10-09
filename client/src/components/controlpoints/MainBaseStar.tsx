import { ControlPoint } from "../../api/_liberationApi";
import { DivIcon, Point } from "leaflet";
import { Marker, Tooltip } from "react-leaflet";

export const STAR_COLOR = "#f2c94c";

/** The gold star marker's icon: a star drawn just above and right of the base. */
export const mainBaseStarIcon = () =>
  new DivIcon({
    className: "",
    html:
      `<div style="color:${STAR_COLOR};font-size:20px;line-height:20px;` +
      `text-shadow:0 0 3px #000,0 0 2px #000;">\u2605</div>`,
    iconSize: new Point(20, 20),
    iconAnchor: new Point(-6, 30),
  });

interface MainBaseStarProps {
  controlPoint: ControlPoint;
}

/**
 * A gold star on a side's main supply base. The player's is always shown; the
 * enemy's only once a flight has struck or scouted it.
 */
export const MainBaseStar = (props: MainBaseStarProps) => {
  if (!props.controlPoint.main_base) {
    return <></>;
  }
  const whose = props.controlPoint.blue ? "Our" : "Enemy";
  return (
    <Marker
      position={props.controlPoint.position}
      icon={mainBaseStarIcon()}
      interactive={true}
      zIndexOffset={1100}
    >
      <Tooltip>{`${whose} main supply base`}</Tooltip>
    </Marker>
  );
};

export default MainBaseStar;
