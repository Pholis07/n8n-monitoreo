# n8n + monitoreo de servicios con alertas a Signal

Proyecto de infraestructura local: una instancia de [n8n](https://n8n.io) corriendo en contenedores, con PostgreSQL como base de datos, y un flujo que revisa cada 5 minutos si una lista de servicios web responde. Si alguno se cae (o vuelve a funcionar), manda un mensaje a Signal.

Solo avisa cuando el estado cambia. Si un servicio sigue caído, no vas a recibir el mismo mensaje cada 5 minutos.

## Qué hay en este directorio

| Archivo | Para qué sirve |
|---|---|
| `docker-compose.yml` | Define los 3 contenedores: `postgres`, `n8n` y `signal-api` |
| `.env` | Contraseñas y llave de cifrado (no se sube a git) |
| `.env.example` | Plantilla del `.env` sin secretos |
| `monitor-servicios.json` | El flujo de monitoreo, listo para importar en n8n |

## Cómo está armado

```
Cada 5 min ─► Lista de servicios ─► Revisar URL ─► Preparar resultado ─► ¿Está caído?
                                                                          │ sí      │ no
                                                                  ¿Recién cayó?  ¿Se recuperó?
                                                                          └────┬────┘
                                                                        Enviar a Signal
```

- **postgres** (`postgres:17-alpine`): guarda los flujos, credenciales y ejecuciones de n8n. No se expone al host, solo n8n lo ve.
- **n8n**: la interfaz web en `http://localhost:5678`.
- **signal-api** ([signal-cli-rest-api](https://github.com/bbernhard/signal-cli-rest-api)): n8n no trae un nodo de Signal, así que este contenedor ofrece una API HTTP para mandar mensajes. Solo escucha en `127.0.0.1:8080`.

Todos los datos viven en volúmenes nombrados: `n8n_postgres17_data`, `n8n_n8n_data` y `n8n_signal_data`.

## Levantar el proyecto

Requisitos: Docker con el plugin compose, o Podman con `podman-docker` y `docker-compose` (así está en esta máquina).

```bash
cd ~/n8n
cp .env.example .env        # solo si no existe el .env
# Genera los secretos:
sed -i "s/^POSTGRES_PASSWORD=.*/POSTGRES_PASSWORD=$(openssl rand -hex 24)/" .env
sed -i "s/^N8N_ENCRYPTION_KEY=.*/N8N_ENCRYPTION_KEY=$(openssl rand -hex 32)/" .env

docker compose up -d
docker compose ps
docker compose logs -f n8n   # espera a ver "Editor is now accessible via"
```

Abre `http://localhost:5678` y crea tu usuario administrador.

> **Importante:** guarda bien la `N8N_ENCRYPTION_KEY`. Con ella n8n cifra las credenciales que guardas. Si la pierdes, las credenciales guardadas ya no se pueden leer.

### Si usas Podman sin root

`restart: unless-stopped` no levanta los contenedores después de reiniciar la máquina a menos que actives esto una vez:

```bash
systemctl --user enable --now podman-restart.service
loginctl enable-linger $USER
```

### Si usas Podman sin root en una laptop: DNS al cambiar de red

Con Podman rootless, el DNS interno de los contenedores (`aardvark-dns`) guarda los servidores DNS de la red que tenías cuando arrancó. Si después te cambias de Wi-Fi, los contenedores dejan de resolver nombres de internet (`EAI_AGAIN`) y **todos los servicios parecen caídos**, aunque no lo estén.

La solución es crear un `docker-compose.override.yml` (compose lo carga solo y está en `.gitignore`) para que n8n y signal-api usen el reenviador DNS de pasta, que siempre sigue al DNS actual del sistema:

```yaml
services:
  n8n:
    dns:
      - 169.254.1.1
  signal-api:
    dns:
      - 169.254.1.1
```

Luego `docker compose up -d`. Con Docker normal no hace falta y no funciona, por eso no está en el `docker-compose.yml` principal.

### Si Docker Hub te bloquea las descargas

Si ves `toomanyrequests: You have reached your unauthenticated pull rate limit`, inicia sesión con una cuenta gratuita de Docker Hub y vuelve a intentar:

```bash
docker login docker.io
docker compose pull
docker compose up -d
```

## Configurar Signal

Necesitas un número de Signal desde el cual el bot mande los mensajes. Hay dos opciones:

### Opción A: vincular tu propio número (la más rápida)

1. Abre en el navegador: `http://localhost:8080/v1/qrcodelink?device_name=n8n`
2. En tu teléfono: Signal, Ajustes, Dispositivos vinculados, el botón **+**, y escanea el QR.
3. Verifica que quedó registrado:
   ```bash
   curl http://localhost:8080/v1/accounts
   ```

Detalle a tomar en cuenta: si el bot manda mensajes desde tu número hacia tu mismo número, llegan a "Nota personal" y normalmente **no suenan como notificación**, porque Signal los trata como mensajes que tú mismo enviaste. Para recibir alertas con sonido, usa la opción B o manda las alertas al número de otra persona.

### Opción B: un número aparte para el bot

Con un chip extra o un número que pueda recibir SMS o llamada:

```bash
# 1. Pide el código (Signal casi siempre pide captcha)
#    Saca el captcha en https://signalcaptchas.org/registration/generate.html
#    y copia el enlace "signalcaptcha://..." que te da.
curl -X POST -H "Content-Type: application/json" \
  -d '{"captcha":"signalcaptcha://PEGA_AQUI"}' \
  'http://localhost:8080/v1/register/+52NUMERO_BOT'

# 2. Confirma con el código que te llegó por SMS
curl -X POST 'http://localhost:8080/v1/register/+52NUMERO_BOT/verify/123456'
```

### Probar que Signal manda mensajes

```bash
curl -X POST -H "Content-Type: application/json" \
  -d '{"message":"Prueba desde n8n","number":"+52NUMERO_BOT","recipients":["+52NUMERO_DESTINO"]}' \
  http://localhost:8080/v2/send
```

Los números van en formato internacional: `+52` seguido de los 10 dígitos.

## Importar el flujo

1. En n8n, entra a **Overview** y crea un flujo nuevo (**Create Workflow**).
2. Menú de los tres puntos arriba a la derecha, **Import from File**, y elige `monitor-servicios.json`.
3. Abre el nodo **Enviar a Signal** y en el campo **JSON** cambia:
   - `+52TU_NUMERO_BOT` por el número registrado en signal-api.
   - `+52NUMERO_DESTINO` por el número que recibe las alertas (puedes poner varios en la lista).
4. Abre el nodo **Lista de servicios** y pon tus URLs reales.
5. Guarda y activa el flujo con el interruptor **Active** (o **Publish**, según tu versión de n8n).

No hace falta crear credenciales en n8n: signal-api está en la red interna de los contenedores y no pide autenticación.

## Agregar más servicios

Todo está en el nodo **Lista de servicios**. Solo agrega una línea por servicio:

```js
const servicios = [
  { nombre: 'Sitio web', url: 'https://ejemplo.com' },
  { nombre: 'API interna', url: 'https://api.ejemplo.com/health' },
  { nombre: 'Panel admin', url: 'http://192.168.1.50:8080' },
  { nombre: 'Mi nuevo servicio', url: 'https://nuevo.ejemplo.com' },
];
```

Algunas cosas a considerar:

- El estado de cada servicio se guarda usando la URL como llave. Si cambias una URL, se toma como un servicio nuevo.
- Para vigilar algo que corre en tu propia máquina, `localhost` no funciona desde dentro del contenedor. Usa `http://host.containers.internal:PUERTO` (Podman) o `http://host.docker.internal:PUERTO` (Docker).
- Se considera caído si responde con un código fuera de 200 a 299, si no contesta en 10 segundos o si no se puede conectar.

## Cómo funciona el "solo avisar cuando cambia"

Los nodos **¿Recién cayó?** y **¿Se recuperó?** guardan el último estado de cada URL en el *Workflow Static Data* de n8n:

- Estaba arriba (o es la primera vez) y ahora está caído: manda 🔴.
- Estaba caído y ahora responde bien: manda 🟢.
- Cualquier otro caso: no manda nada.

Ojo: n8n **solo guarda el Static Data en ejecuciones de producción**, es decir, cuando el flujo está activo y lo dispara el Schedule. Si lo pruebas con **Execute Workflow** a mano, va a mandar la alerta cada vez porque no recuerda el estado anterior. Eso es normal.

## Respaldos

Crea la carpeta de respaldos (ya está en `.gitignore`):

```bash
mkdir -p ~/n8n/backups && cd ~/n8n
```

### Base de datos (lo más importante)

Un respaldo lógico con `pg_dump` es más seguro que copiar los archivos de Postgres en caliente:

```bash
docker compose exec -T postgres pg_dump -U n8n -d n8n -Fc > backups/n8n-db-$(date +%F).dump
```

Restaurar:

```bash
docker compose exec -T postgres pg_restore -U n8n -d n8n --clean --if-exists < backups/n8n-db-AAAA-MM-DD.dump
```

### Volúmenes de n8n y Signal

```bash
docker compose stop n8n signal-api
for v in n8n_data signal_data; do
  docker run --rm -v n8n_${v}:/data:ro -v "$PWD/backups":/backup \
    docker.io/library/alpine tar czf /backup/${v}-$(date +%F).tar.gz -C /data .
done
docker compose start n8n signal-api
```

Restaurar un volumen:

```bash
docker compose stop n8n
docker run --rm -v n8n_n8n_data:/data -v "$PWD/backups":/backup \
  docker.io/library/alpine sh -c "rm -rf /data/* && tar xzf /backup/n8n_data-AAAA-MM-DD.tar.gz -C /data"
docker compose start n8n
```

Guarda también una copia del `.env` en un lugar seguro (por ejemplo, tu gestor de contraseñas). Sin la `N8N_ENCRYPTION_KEY` el respaldo de la base de datos no sirve para las credenciales.

## Actualizar Postgres a otra versión mayor

Los datos de Postgres no son compatibles entre versiones mayores (por ejemplo de 16 a 17), así que no basta con cambiar la imagen. Así se hizo el cambio de 16 a 17 en este proyecto:

```bash
# 1. Respaldo con la versión vieja todavía corriendo
docker compose stop n8n
docker compose exec -T postgres pg_dump -U n8n -d n8n -Fc > backups/n8n-db-antes.dump

# 2. En docker-compose.yml: cambia la imagen (postgres:17-alpine) y usa un volumen
#    nuevo (postgres17_data) para no pisar los datos viejos
docker compose stop postgres && docker compose rm -f postgres
docker compose up -d postgres

# 3. Restaura y levanta n8n
docker compose exec -T postgres pg_restore -U n8n -d n8n --exit-on-error < backups/n8n-db-antes.dump
docker compose up -d
```

El volumen viejo queda sin usar. Cuando confirmes que todo funciona, puedes borrarlo con `docker volume rm n8n_postgres_data`.

## Comandos útiles

```bash
docker compose ps                 # estado de los contenedores
docker compose logs -f n8n        # logs de n8n
docker compose pull && docker compose up -d   # actualizar imágenes
docker compose down               # apagar (los datos se quedan en los volúmenes)
```
