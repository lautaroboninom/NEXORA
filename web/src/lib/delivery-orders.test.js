import test from "node:test";
import assert from "node:assert/strict";
import {
  deliveryOrderAppendRentalEquipment,
  deliveryOrderBejermanCustomerLabel,
  deliveryOrderDefaultDeposit,
  deliveryOrderFindStockLot,
  deliveryOrderIsOccasionalCustomer,
  deliveryOrderOccasionalCustomerDocument,
  deliveryOrderOccasionalCustomerReady,
  deliveryOrderPartidaIsFixedEquipment,
  deliveryOrderRentalEquipmentKey,
  deliveryOrderRentalLotBlockReason,
  deliveryOrderSelectedRentalKeys,
  deliveryOrderSplitRentalLots,
  deliveryOrderUsedPartidaKeys,
  isDeliveryOrderBlankItem,
  isOccasionalCustomerName,
} from "./delivery-orders.js";

test("un NS con stock en STL que NEXORA no deja alquilar no se ofrece como disponible", () => {
  // E2321905419: stock 1 en STL por un remito manual que no lo descontó, pero alquilado desde 2024.
  const lots = [
    { partida: "MZJ5S121199", rentalAvailable: true },
    {
      partida: "E2321905419",
      rentalAvailable: false,
      rentalBlockReason: "El NS E2321905419 está en la OS-25863, que ya figura alquilada.",
    },
    { partida: "L-VAL-1" },
  ];

  const { usable, blocked } = deliveryOrderSplitRentalLots(lots);

  assert.deepEqual(usable.map((lot) => lot.partida), ["MZJ5S121199", "L-VAL-1"]);
  assert.deepEqual(blocked.map((lot) => lot.partida), ["E2321905419"]);
  assert.equal(
    deliveryOrderRentalLotBlockReason(blocked[0]),
    "El NS E2321905419 está en la OS-25863, que ya figura alquilada.",
  );
});

test("un NS bloqueado sin motivo informado igual dice que no se puede alquilar", () => {
  assert.equal(
    deliveryOrderRentalLotBlockReason({ partida: "X1", rentalAvailable: false }),
    "El NS X1 no está disponible para alquilar.",
  );
  assert.equal(deliveryOrderRentalLotBlockReason({ partida: "X1" }), "");
  assert.deepEqual(deliveryOrderSplitRentalLots(null), { usable: [], blocked: [] });
});

test("una partida que no está en un listado recortado no se da por faltante", () => {
  // OE-00478: el listado de SAR cortaba en 100 partidas y E23BE703521 quedaba afuera con stock.
  const lots = [{ partida: "E23BD501024", stockAvailableQuantity: 1 }];

  assert.deepEqual(deliveryOrderFindStockLot(lots, " e23bd501024 "), { lot: lots[0], missing: false });
  assert.deepEqual(deliveryOrderFindStockLot(lots, "E23BE703521"), { lot: null, missing: true });
  assert.deepEqual(deliveryOrderFindStockLot(lots, "E23BE703521", { truncated: true }), { lot: null, missing: false });
  assert.deepEqual(deliveryOrderFindStockLot(null, "E23BE703521", {}), { lot: null, missing: true });
});

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

test("las claves de equipos de alquiler identifican OS o deviceId", () => {
  assert.equal(deliveryOrderRentalEquipmentKey({ ingresoId: 123 }), "os:123");
  assert.equal(deliveryOrderRentalEquipmentKey({ deviceId: 456 }), "dev:456");
  assert.equal(deliveryOrderRentalEquipmentKey({ ingresoId: 123, deviceId: 456 }), "os:123");
  assert.equal(deliveryOrderRentalEquipmentKey({}), "");
});

test("selectedRentalKeys incluye equipos tanto a nivel renglón como dentro de sus partidas", () => {
  const items = [
    {
      articleCode: "1111001",
      ingresoId: "",
      deviceId: "",
      partidas: [
        { ingresoId: 101, deviceId: 1, partida: "CP001" },
        { ingresoId: 102, deviceId: 2, partida: "CP002" },
      ],
    },
    {
      articleCode: "1105001",
      ingresoId: "201",
      deviceId: "3",
      partidas: [{ ingresoId: "201", deviceId: "3", partida: "SP001" }],
    },
  ];

  const keys = deliveryOrderSelectedRentalKeys(items);
  assert.equal(keys.has("os:101"), true);
  assert.equal(keys.has("os:102"), true);
  assert.equal(keys.has("os:201"), true);
  assert.equal(keys.has("os:999"), false);
});

test("atajo alquiler: agregar dos equipos con el mismo artículo los consolida como partidas en el mismo renglón", () => {
  const initialItems = [
    {
      id: "",
      quantity: "1",
      articleCode: "",
      description: "",
      partidas: [{ partida: "", assignedQuantity: "1" }],
    },
  ];

  assert.equal(isDeliveryOrderBlankItem(initialItems[0]), true);

  const equip1 = {
    ingresoId: 101,
    deviceId: 1,
    articleCode: "1111001",
    articleName: "HUMIDIFICADOR",
    equipmentSerial: "HUM-001",
    accessories: "Manguera y cámara",
    stockAvailableQuantity: 1,
  };

  const equip2 = {
    ingresoId: 102,
    deviceId: 2,
    articleCode: "1111001",
    articleName: "HUMIDIFICADOR",
    equipmentSerial: "HUM-002",
    accessories: "Manguera y cámara",
    stockAvailableQuantity: 1,
  };

  // 1. Agregar el primer equipo: reemplaza el renglón inicial vacío
  const res1 = deliveryOrderAppendRentalEquipment(initialItems, equip1);
  assert.equal(res1.added, true);
  assert.equal(res1.targetIndex, 0);
  assert.equal(res1.items.length, 1);
  assert.equal(res1.items[0].articleCode, "1111001");
  assert.equal(res1.items[0].quantity, "1");
  assert.equal(res1.items[0].remitoArticleLegend, "Manguera y cámara");
  assert.equal(res1.items[0].partidas.length, 1);
  assert.equal(res1.items[0].partidas[0].partida, "HUM-001");
  assert.equal(res1.items[0].partidas[0].ingresoId, "101");

  // 2. Agregar el segundo equipo del MISMO artículo: no crea otro renglón, suma la partida y actualiza cantidad a 2
  const res2 = deliveryOrderAppendRentalEquipment(res1.items, equip2);
  assert.equal(res2.added, true);
  assert.equal(res2.targetIndex, 0);
  assert.equal(res2.items.length, 1, "No debe agregar un renglón separado");
  assert.equal(res2.items[0].quantity, "2", "La cantidad debe actualizarse al total de partidas");
  assert.equal(res2.items[0].partidas.length, 2, "Debe tener 2 partidas asignadas");
  assert.equal(res2.items[0].partidas[0].partida, "HUM-001");
  assert.equal(res2.items[0].partidas[1].partida, "HUM-002");
  assert.equal(res2.items[0].partidas[1].ingresoId, "102");
  assert.equal(res2.items[0].remitoArticleLegend, "Manguera y cámara");

  // 3. Agregar un equipo de OTRO artículo (ej. CPAP 1105001): crea un segundo renglón
  const equip3 = {
    ingresoId: 201,
    deviceId: 3,
    articleCode: "1105001",
    articleName: "CPAP RESMED",
    equipmentSerial: "CPAP-999",
    accessories: "Bolso y transformador",
    stockAvailableQuantity: 1,
  };

  const res3 = deliveryOrderAppendRentalEquipment(res2.items, equip3);
  assert.equal(res3.added, true);
  assert.equal(res3.targetIndex, 1);
  assert.equal(res3.items.length, 2, "Debe agregar un renglón para el nuevo artículo");
  assert.equal(res3.items[1].articleCode, "1105001");
  assert.equal(res3.items[1].quantity, "1");
  assert.equal(res3.items[1].partidas.length, 1);
  assert.equal(res3.items[1].partidas[0].partida, "CPAP-999");
  assert.equal(res3.items[1].remitoArticleLegend, "Bolso y transformador");

  // 4. Intentar agregar de nuevo un equipo ya presente no duplica
  const resDup = deliveryOrderAppendRentalEquipment(res3.items, equip1);
  assert.equal(resDup.added, false);
  assert.equal(resDup.items[0].partidas.length, 2);
});

test("atajo alquiler: si el renglón existente tenía cantidad mayor y fila vacía, ocupa la fila y preserva la cantidad", () => {
  const items = [
    {
      id: "item-1",
      quantity: "5",
      articleCode: "1111001",
      description: "Humidificador",
      remitoArticleLegend: "",
      partidas: [{ partida: "", assignedQuantity: "1" }],
    },
  ];

  const equip = {
    ingresoId: 101,
    deviceId: 1,
    articleCode: "1111001",
    articleName: "HUMIDIFICADOR",
    equipmentSerial: "HUM-001",
    accessories: "Manguera",
  };

  const res = deliveryOrderAppendRentalEquipment(items, equip);
  assert.equal(res.added, true);
  assert.equal(res.items.length, 1);
  assert.equal(res.items[0].quantity, "5", "Preserva la cantidad 5 preestablecida");
  assert.equal(res.items[0].partidas.length, 1);
  assert.equal(res.items[0].partidas[0].partida, "HUM-001");
  assert.equal(res.items[0].remitoArticleLegend, "Manguera");
});
