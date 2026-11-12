# MerMarKids Ventas

App web simple para cargar pedidos y gastos de MerMarKids en una sola pantalla.

El archivo principal de la app es `index.html` y los datos se guardan en SQLite mediante `server.py`.

## Funciones

- Alta de pedidos con fecha del dia.
- Calculo automatico de total, seña y resta a abonar.
- Busqueda y modificacion de pedidos por numero de orden.
- Estado del pedido: `GRAFICA`, `ENTREGA`, `PENDIENTE DE ENTREGA`, `EN PAÑO`.
- Cuentas disponibles: `MARIAN MP`, `MARIAN EF`, `MER MP`, `MER EF`.
- Alta de gastos con efectivo, Mercado Pago y cuenta responsable.
- Resumen de ventas, señas/cobros, resta a cobrar, gastos y caja.
- Exportacion de pedidos y gastos a CSV.
- Buscador y filtros en las tablas de pedidos (cliente, producto, orden, estado, pago) y gastos (concepto, cuenta), con totales al pie.
- Historial de pagos de cada pedido al editarlo, con opcion de quitar un pago cargado por error.
- Comprobante de pedido descargable en texto.

## Novedades de esta version

- La fecha del dia se toma en hora local (antes, despues de las 21 hs, se cargaba la fecha del dia siguiente).
- Validaciones claras en pantalla y en el servidor (sin errores silenciosos).
- Proteccion contra doble guardado al hacer varios clics.
- El servidor solo publica `index.html` y `assets/` (ya no expone `server.py` ni otros archivos).
- Conexiones a la base cerradas correctamente y modo WAL para varios usuarios a la vez.
- Endpoint `/health` y chequeo de salud en Docker.
- CSV con fechas `dd/mm/aaaa` y decimales con coma para abrir bien en Excel.

La base de datos es la misma: no hace falta migrar nada, los datos actuales se conservan.

Los datos se guardan en una base SQLite dentro del volumen Docker `mermarkids_data`, asi todos los navegadores ven la misma informacion.

## Ejecutar con Docker

```bash
docker compose up -d --build
```

Luego abrir:

```text
http://localhost:8015
```

Para cambiar el puerto:

```bash
APP_PORT=8090 docker compose up -d --build
```

## Portainer

1. Subir este proyecto a GitHub.
2. Entrar a Portainer.
3. Ir a `Stacks`.
4. Crear un stack nuevo.
5. Elegir `Repository`.
6. Pegar la URL del repo de GitHub.
7. Usar `docker-compose.yml` como compose path.
8. Deploy the stack.

Por defecto la app queda expuesta en el puerto `8015`.

Si Portainer muestra un error tipo `"/app": not found`, el repo todavia tiene una version vieja del `Dockerfile` o no se subio el archivo `index.html` a la raiz.

## Persistencia

El compose crea este volumen:

```text
mermarkids_data
```

No lo borres desde Portainer si queres conservar pedidos y gastos.
