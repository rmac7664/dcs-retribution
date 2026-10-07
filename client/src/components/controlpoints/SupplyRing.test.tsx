import { ControlPoint } from "../../api/_liberationApi";
import LocationTooltipText from "./LocationTooltipText";
import SupplyRing, { supplyColor } from "./SupplyRing";
import { render, screen } from "@testing-library/react";
import { LatLng } from "leaflet";

const mockCircleMarker = jest.fn();
jest.mock("react-leaflet", () => ({
  CircleMarker: (props: any) => {
    mockCircleMarker(props);
    return null;
  },
}));

function base(supply?: ControlPoint["supply"]): ControlPoint {
  return {
    id: "foo",
    name: "Senaki",
    blue: true,
    position: new LatLng(42, 42),
    mobile: false,
    sidc: "",
    supply: supply,
  };
}

describe("SupplyRing", () => {
  beforeEach(() => mockCircleMarker.mockClear());

  it("is coloured by supply level", () => {
    expect(supplyColor(80)).toBe("#3fb950");
    expect(supplyColor(50)).toBe("#d29922");
    expect(supplyColor(10)).toBe("#f85149");
  });

  it("draws nothing for bases without a supply report", () => {
    render(<SupplyRing controlPoint={base()} />);
    expect(mockCircleMarker).not.toHaveBeenCalled();
  });

  it("draws a dashed ring for a depot", () => {
    render(
      <SupplyRing
        controlPoint={base({
          munitions_percent: 20,
          fuel_percent: 90,
          level: 20,
          depot: true,
          inbound_runs: 1,
          lines: [],
        })}
      />,
    );
    expect(mockCircleMarker).toHaveBeenCalledWith(
      expect.objectContaining({
        pathOptions: expect.objectContaining({
          color: "#f85149",
          dashArray: "6 4",
        }),
      }),
    );
  });
});

describe("LocationTooltipText", () => {
  it("lists the supply lines under the name", () => {
    const { container } = render(
      <LocationTooltipText
        name="Senaki"
        lines={["Munitions 20% of authorized", "Supply depot"]}
      />,
    );
    expect(screen.getByText("Senaki")).toBeTruthy();
    expect(container.textContent).toContain("Munitions 20% of authorized");
    expect(container.textContent).toContain("Supply depot");
  });
});
