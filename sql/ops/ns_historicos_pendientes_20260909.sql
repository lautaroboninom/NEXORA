-- Sólo lectura en SBDASEP. Contrasta los RDA pendientes sin alterar datos.
-- scv_FecMod es fecha de modificación, no prueba por sí sola la fecha de creación.
SELECT c.scv_ID, c.scv_FEmision, c.scv_FIngreso, c.scv_FecMod,
       c.scvusu_Codigo, c.scvcli_Cod, c.scvtop_Cod, c.scv_ActStock,
       t.spvtco_Cod, t.spv_Letra, t.spv_CodPvt, t.spv_Nro,
       d.sdv_ID, d.sdv_NReng, d.sdv_TipoIt, d.sdvart_CodGen,
       d.sdvstp_Partida, d.sdv_CantUM1, d.sdv_Desc, d.sdvmst_ID
FROM dbo.SegCabV c
JOIN dbo.SegDetV d
  ON d.sdvemp_Codigo=c.scvemp_Codigo
 AND d.sdvsuc_Cod=c.scvsuc_Cod AND d.sdvscv_ID=c.scv_ID
LEFT JOIN dbo.SegTiposV t
  ON t.spvemp_Codigo=c.scvemp_Codigo
 AND t.spvsuc_Cod=c.scvsuc_Cod AND t.spvscv_ID=c.scv_ID
 AND t.spv_OrdenLis=1
WHERE c.scvemp_Codigo='SEP'
  AND c.scv_ID IN (8802,16919,19178,19124)
ORDER BY c.scv_ID,d.sdv_NReng;
