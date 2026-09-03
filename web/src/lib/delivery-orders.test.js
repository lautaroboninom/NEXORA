import test from "node:test";
import assert from "node:assert/strict";
import {
  deliveryOrderBejermanCustomerLabel,
  deliveryOrderDefaultDeposit,
  deliveryOrderPartidaIsFixedEquipment,
  deliveryOrderIsOccasionalCustomer,
  deliveryOrderOccasionalCustomerDocument,
  deliveryOrderOccasionalCustomerReady,
  isOccasionalCustomerName,
  deliveryOrderUsedPartidaKeys,
} from "./delivery-orders.js";

test("reconoce los nombres locales que marcan un cliente ocasional", () => {
  assert.equal(isOccasionalCustomerName("Particular"), true);
  assert.equal(isOccasionalCustomerName("  pärticular "), true);
  assert.equal(isOccasionalCustomerName("Consumidor Final"), true);
  assert.equal(isOccasionalCustomerName("Cliente Vario"), true);
  assert.equal(isOccasionalCustomerName("SEDESA S.A."), false);
  assert.equal(isOccasionalCustomerName(""), false);
});

test("una orden es ocasional solo si es un particular sin código Bejerman", () => {
  assert.equal(deliveryOrderIsOccasionalCustomer({ customerName: "Particular", bejermanCustomerCode: "" }), true);
  // El código cargado a mano manda: si existe en Bejerman no se emite como ocasional.
  assert.equal(deliveryOrderIsOccasionalCustomer({ customerName: "Particular", bejermanCustomerCode: "PELLEG" }), false);
  // Un cliente real sin mapear no es un ocasional, le falta el mapeo.
  assert.equal(deliveryOrderIsOccasionalCustomer({ customerName: "SEDESA S.A.", bejermanCustomerCode: "" }), false);
});

test("el ocasional necesita nombre y documento del propietario", () => {
  assert.equal(deliveryOrderOccasionalCustomerReady({ propietarioNombre: "Jorge Brunasco", propietarioDoc: "27858174" }), true);
  assert.equal(deliveryOrderOccasionalCustomerReady({ propietarioNombre: "Jorge Brunasco", propietarioDoc: "" }), false);
  assert.equal(deliveryOrderOccasionalCustomerReady({ propietarioNombre: "", propietarioDoc: "27858174" }), false);
});

test("el documento del ocasional se normaliza a dígitos y sólo aplica a particulares", () => {
  // El maestro guarda el documento sin puntos; el ingreso puede traerlo con formato.
  assert.equal(
    deliveryOrderOccasionalCustomerDocument({ customerName: "Particular", propietarioDoc: "30.710.698" }),
    "30710698",
  );
  assert.equal(
    deliveryOrderOccasionalCustomerDocument({ customerName: "Particular", propietarioDoc: "30710698" }),
    "30710698",
  );
  // Un cliente con código no se factura como ocasional aunque el ingreso tenga propietario.
  assert.equal(
    deliveryOrderOccasionalCustomerDocument({
      customerName: "SEDESA S.A.",
      bejermanCustomerCode: "SEDE",
      propietarioDoc: "30710698",
    }),
    "",
  );
  assert.equal(deliveryOrderOccasionalCustomerDocument({ customerName: "Particular" }), "");
});

test("el rótulo de cliente Bejerman muestra el propietario del ocasional", () => {
  assert.equal(deliveryOrderBejermanCustomerLabel({ bejermanCustomerCode: "SEDE" }), "SEDE");
  assert.equal(
    deliveryOrderBejermanCustomerLabel({
      customerName: "Particular",
      bejermanCustomerCode: "",
      propietarioNombre: "Jorge Brunasco",
      propietarioDoc: "27858174",
    }),
    "Cliente ocasional - Jorge Brunasco - Doc. 27858174",
  );
  assert.equal(deliveryOrderBejermanCustomerLabel({ customerName: "Particular" }), "Cliente ocasional");
  assert.equal(deliveryOrderBejermanCustomerLabel({ customerName: "SEDESA S.A." }), "");
});

test("excluye de las sugerencias las partidas usadas arriba para el mismo articulo y deposito", () => {
  const items = [
    {
      articleCode: "1101003",
      stockDepositCode: "VAL",
      partidas: [{ partida: "D1-1489", stockDepositCode: "VAL" }],
    },
    {
      articleCode: "1101003",
      stockDepositCode: "VAL",
      partidas: [{ partida: "", stockDepositCode: "VAL" }],
    },
  ];

  assert.deepEqual(
    [...deliveryOrderUsedPartidaKeys(items, 1, 0, "VAL")],
    ["d1-1489"],
  );
});

test("cada tipo de entrega sale de su propio deposito", () => {
  // Espejo de remito_profile_for_type: una reparación sale de STC, no del depósito de venta.
  assert.equal(deliveryOrderDefaultDeposit("service_release"), "STC");
  assert.equal(deliveryOrderDefaultDeposit("rental"), "STL");
  assert.equal(deliveryOrderDefaultDeposit("sale"), "VAL");
  assert.equal(deliveryOrderDefaultDeposit("demo"), "VAL");
  // Acepta la orden entera y cae en el depósito central si el tipo no se conoce.
  assert.equal(deliveryOrderDefaultDeposit({ deliveryType: "service_release" }), "STC");
  assert.equal(deliveryOrderDefaultDeposit(""), "VAL");
  assert.equal(deliveryOrderDefaultDeposit(undefined), "VAL");
});

test("solo la reparacion tiene la partida atada al equipo del cliente", () => {
  assert.equal(deliveryOrderPartidaIsFixedEquipment("service_release"), true);
  assert.equal(deliveryOrderPartidaIsFixedEquipment({ deliveryType: "service_release" }), true);
  assert.equal(deliveryOrderPartidaIsFixedEquipment("sale"), false);
  assert.equal(deliveryOrderPartidaIsFixedEquipment("rental"), false);
});
