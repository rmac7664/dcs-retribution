import { ControlPoint } from "../../api/_liberationApi";
import MainBaseStar from "./MainBaseStar";
import { render } from "@testing-library/react";
import { LatLng } from "leaflet";

const mockMarker = jest.fn();
jest.mock("react-leaflet", () => ({
  Marker: (props: any) => {
    mockMarker(props);
    return <div>{props.children}</div>;
  },
  Tooltip: (props: any) => <span>{props.children}</span>,
}));

function base(blue: boolean, main_base?: boolean): ControlPoint {
  return {
    id: "foo",
    name: "Senaki",
    blue: blue,
    position: new LatLng(42, 42),
    mobile: false,
    sidc: "",
    main_base: main_base,
  };
}

describe("MainBaseStar", () => {
  beforeEach(() => mockMarker.mockClear());

  it("draws nothing for ordinary bases", () => {
    render(<MainBaseStar controlPoint={base(true)} />);
    expect(mockMarker).not.toHaveBeenCalled();
  });

  it("stars our main base", () => {
    const { getByText } = render(
      <MainBaseStar controlPoint={base(true, true)} />,
    );
    expect(mockMarker).toHaveBeenCalledTimes(1);
    expect(getByText("Our main supply base")).toBeTruthy();
  });

  it("stars a found enemy main base", () => {
    const { getByText } = render(
      <MainBaseStar controlPoint={base(false, true)} />,
    );
    expect(getByText("Enemy main supply base")).toBeTruthy();
  });
});
