-- Sólo lectura. Reproducción del SQL real recuperado de consEspec0182 el 08/09/2026.
-- Ejecutar en SBDPSEP (prueba) o SBDASEP (real). No crea tablas ni modifica datos.
WITH NS_Tipos AS (SELECT spvemp_Codigo, spvsuc_Cod, spvscv_ID
, spvtco_Circuito AS spvtco_Circuito1
, spvtco_Circuito AS spvtco_Circuito2
, SPV_CODPVT AS SPV_CODPVT1, SPV_CODPVT AS SPV_CODPVT2, SPV_ESTADOAUTORIZ AS SPV_ESTADOAUTORIZ1, SPV_ESTADOAUTORIZ AS SPV_ESTADOAUTORIZ2, SPV_LETRA AS SPV_LETRA1, SPV_LETRA AS SPV_LETRA2, SPV_NRO AS SPV_NRO1, SPV_NRO AS SPV_NRO2, SPV_ORIG AS SPV_ORIG1, SPV_ORIG AS SPV_ORIG2, SPVTCO_COD AS SPVTCO_COD1, SPVTCO_COD AS SPVTCO_COD2, SPVTCO_TIPOFIJO AS SPVTCO_TIPOFIJO1, SPVTCO_TIPOFIJO AS SPVTCO_TIPOFIJO2
 
 FROM SegTiposV
 WHERE spv_OrdenLis = 1), NS_Items AS (SELECT 
 DA1_DESC, (CASE SDV_TIPOIT
 WHEN 'A' THEN 'ARTÍCULO'
 WHEN 'C' THEN 'CONCEPTO'
 WHEN 'L' THEN 'LEYENDA'
 WHEN 'D' THEN 'DESCUENTO'
 ELSE            'TIPO DE ITEM INEXISTENTE' END) AS DESCTIPOIT, DAY(SCV_FEMISION) AS DIAEMISION, SPVTCO_COD1 + ' '
 + (SELECT CASE WHEN SPV_LETRA1 <> ' ' THEN SPV_LETRA1 + ' ' ELSE ' ' END)
 + (SELECT CASE WHEN SPV_CODPVT1 <> ' ' THEN SPV_CODPVT1 + '-' ELSE ' ' END)
 + SPV_NRO1 AS IDENTCOMP1, MON_SIMBOLO, SCV_ACTSTOCK, SCV_FEMISION, SCV_FINGRESO, SCVCLI_COD, SCVCLI_RAZSOC, SDV_CANTUM1, SDV_DESC, SDV_IMPTOT, SDV_PRECIOUN, SDVART_CODGEN, SDVCON_COD, SDVSTP_PARTIDA, SDVUME_DESC1, SPVTCO_COD1, STP_OBS, TOP_DESC

 
 FROM ParamGen, ((((((((((SegDetV
         LEFT JOIN ((Articulos
           LEFT JOIN Defi1Art 
          ON Articulos.artda1_Cod = Defi1Art.da1_Cod) 
          LEFT JOIN Defi2Art 
         ON Articulos.artda2_Cod = Defi2Art.da2_Cod) 
        ON SegDetV.sdvart_CodGen = Articulos.art_CodGen AND SegDetV.sdvart_CodEle1 = Articulos.art_CodEle1 AND SegDetV.sdvart_CodEle2 = Articulos.art_CodEle2 AND SegDetV.sdvart_CodEle3 = Articulos.art_CodEle3)
        LEFT JOIN Conceptos 
       ON SegDetV.sdvcon_Cod = Conceptos.con_Cod)
       LEFT JOIN BonDtoXArt 
      ON SegDetV.sdvbda_Cod = BonDtoXArt.bda_Cod)
      LEFT JOIN NoGrav 
     ON SegDetV.sdvngr_Cod = NoGrav.ngr_Cod)
     LEFT JOIN Deposito 
    ON SegDetV.sdvdep_Cod = Deposito.dep_Cod)
    LEFT JOIN StockPar 
   ON SegDetV.sdvart_CodGen = StockPar.stpart_CodGen AND SegDetV.sdvart_CodEle1 = StockPar.stpart_CodEle1 AND SegDetV.sdvart_CodEle2 = StockPar.stpart_CodEle2 AND SegDetV.sdvart_CodEle3 = StockPar.stpart_CodEle3 AND SegDetV.sdvdep_Cod = StockPar.stpdep_Cod AND SegDetV.sdvstp_Partida = StockPar.stp_Partida) 
   LEFT JOIN LugarEnt 
  ON SegDetV.sdvlen_ID = LugarEnt.len_ID)
  LEFT JOIN EstadoPedidoProd 
 ON SegDetV.sdvepp_Codigo = EstadoPedidoProd.epp_Codigo)
 LEFT JOIN Defi1Items ON SegDetV.sdvdi1_Cod = Defi1Items.di1_Cod)
 LEFT JOIN Defi2Items ON SegDetV.sdvdi2_Cod = Defi2Items.di2_Cod)
 INNER JOIN ((((((((((((((((((((((((((((((SegCabV
                               LEFT JOIN Clientes 
                              ON SegCabV.scvcli_Cod = Clientes.cli_Cod)
                              LEFT JOIN emp 
                             ON SegCabV.scvemp_Codigo = emp.emp_Codigo)
                             LEFT JOIN Sucursal 
                            ON SegCabV.scvemp_Codigo = Sucursal.sucemp_Codigo AND SegCabV.scvsuc_Cod = Sucursal.suc_Cod)
                            LEFT JOIN PtoTrabajo 
                           ON SegCabV.scvptr_Cod = PtoTrabajo.ptr_Cod)
                           LEFT JOIN PuntoReg 
                          ON SegCabV.scvpre_Cod = PuntoReg.pre_Cod)
                          LEFT JOIN TipoOper 
                         ON SegCabV.scvtop_Cod = TipoOper.top_Cod)
                         LEFT JOIN SitIVA 
                        ON SegCabV.scvsiv_Cod = SitIVA.siv_Cod)
                        LEFT JOIN prv AS prv1 
                       ON SegCabV.scvprv_Codigo = prv1.prv_Codigo)
                       LEFT JOIN prv AS prv2 
                      ON SegCabV.scvprv_CodigoIB = prv2.prv_Codigo)
                      LEFT JOIN mon 
                     ON SegCabV.scvmon_Codigo = mon.mon_Codigo)
                     LEFT JOIN TipoDocum 
                    ON SegCabV.scvtdc_Cod = TipoDocum.tdc_Cod)
                    LEFT JOIN Vendedor 
                   ON SegCabV.scvven_Cod = Vendedor.ven_Cod)
                   LEFT JOIN DefListP 
                  ON SegCabV.scvdlp_Cod = DefListP.dlp_Cod)
                  LEFT JOIN DescCom 
                 ON SegCabV.scvdco_Cod = DescCom.dco_Cod)
                 LEFT JOIN CondVta 
                ON SegCabV.scvcvt_Cod = CondVta.cvt_Cod)
                LEFT JOIN DescFin 
               ON SegCabV.scvdfi_Cod = DescFin.dfi_Cod)
               LEFT JOIN DescRecVs 
              ON SegCabV.scvdrv_Cod = DescRecVs.drv_Cod)
              LEFT JOIN CausaEmi 
             ON SegCabV.scvcem_Cod = CausaEmi.cem_Cod)
             LEFT JOIN Transporte 
            ON SegCabV.scvtrn_Cod = Transporte.trn_Cod)
            LEFT JOIN Apertura 
           ON SegCabV.scvape_CodIN = Apertura.ape_Cod)
           LEFT JOIN Defi1Cli 
          ON SegCabV.scvdc1_Cod = Defi1Cli.dc1_Cod)
          LEFT JOIN Defi2Cli 
         ON SegCabV.scvdc2_Cod = Defi2Cli.dc2_Cod)
         LEFT JOIN TipCli 
        ON SegCabV.scvtic_Cod = TipCli.tic_Cod)
        LEFT JOIN Zona 
       ON SegCabV.scvzon_Cod = Zona.zon_Cod)
       LEFT JOIN usu 
      ON SegCabV.scvusu_Codigo = usu.usu_Codigo)
      LEFT JOIN ContratoPer 
     ON SegCabV.scvcpr_Cod = ContratoPer.cpr_Cod)
     LEFT JOIN Grupos 
    ON SegCabV.scvgrp_Cod = Grupos.grp_Cod)
    LEFT JOIN Proyectos 
   ON SegCabV.scvpry_Cod = Proyectos.pry_Cod)
   LEFT JOIN Paises 
  ON SegCabV.scvpai_Cod = Paises.pai_Cod)
  INNER JOIN ((NS_Tipos
    LEFT JOIN (TipComp AS TipComp1
     LEFT JOIN Defi1 AS Defi11 
    ON TipComp1.tco_Circuito = Defi11.dt1_Circuito AND TipComp1.tco_TipoFijo = Defi11.dt1_TipoFijo) 
   ON NS_Tipos.spvtco_Circuito1 = TipComp1.tco_Circuito AND NS_Tipos.spvtco_Cod1 = TipComp1.tco_Cod) 
   LEFT JOIN (TipComp AS TipComp2 
    LEFT JOIN Defi1 AS Defi12 
   ON TipComp2.tco_Circuito = Defi12.dt1_Circuito AND TipComp2.tco_TipoFijo = Defi12.dt1_TipoFijo) 
  ON NS_Tipos.spvtco_Circuito2 = TipComp2.tco_Circuito AND NS_Tipos.spvtco_Cod2 = TipComp2.tco_Cod) 
 ON SegCabV.scvemp_Codigo = NS_Tipos.spvemp_Codigo AND SegCabV.scvsuc_Cod = NS_Tipos.spvsuc_Cod AND SegCabV.scv_ID = NS_Tipos.spvscv_ID)
ON SegDetV.sdvemp_Codigo = SegCabV.scvemp_Codigo AND SegDetV.sdvsuc_Cod = SegCabV.scvsuc_Cod AND SegDetV.sdvscv_ID = SegCabV.scv_ID

 WHERE  (  (top_Desc LIKE '%Alq%' ) AND (sdv_CantUM1 <> 0 AND NOT sdv_CantUM1 is Null ) AND (scv_ActStock = 1 ) AND (da1_Desc LIKE '%Equipa%' )

 )) SELECT SCVCLI_COD,SCVCLI_RAZSOC,SDVART_CODGEN,SDV_DESC,SDVSTP_PARTIDA,SUM(SDV_CANTUM1) AS SDV_CANTUM1 FROM NS_Items WHERE SDVSTP_PARTIDA='MZJ5S121193' GROUP BY SCVCLI_COD,SCVCLI_RAZSOC,SDVART_CODGEN,SDV_DESC,SDVSTP_PARTIDA

