# Run iCode in a browser

`icode serve` displays the iCode terminal user interface (TUI) in a browser, where you can chat, approve tool calls, and change configuration.

Run `icode serve --help` to view startup options.

This guide calls the device running the browser the “local device” and the machine running the iCode service the “server.” The server can be the local device or a remote server accessed over a network.

Before you begin, [install iCode](../../start/getting-started.md#1-install-icode) on the server. The browser provides the interface; iCode uses the server's configuration and reads and modifies files and runs commands with the permissions of the system user who started the service. All these operations take place on the server. All visitors share its configuration, files, and sessions; they do not receive separate accounts.

After your first login, if no model is configured, [configure a model](../../start/getting-started.md#3-configure-a-model) in the browser interface before starting a conversation. Keep the page connected while a task is running. Closing or refreshing the page, or losing the connection, interrupts any task running in that page.

Choose the section that matches the server's location and how you want to access it:

| Scenario | Instructions |
| --- | --- |
| Run and access iCode locally | [Start iCode locally](#start-icode-locally) |
| Access iCode on a remote server over SSH | [Access remote iCode from your local device](#access-remote-icode-from-your-local-device) |
| Access iCode through a fixed HTTPS domain | [Access iCode through a fixed HTTPS domain](#access-icode-through-a-fixed-https-domain) |

## Start iCode locally

### 1. Start the service

In a terminal on your local device, change to your working directory and start the service:

```shell
icode serve --auth-required
```

Follow the terminal prompts to enter and confirm a browser login password. The password cannot be empty. `--auth-required` enables password login; without an authentication option, access does not require login.

The service is running when the terminal displays `Serving iCode TUI on ...` and an access URL.

### 2. Log in through the browser

Open `http://localhost:7777` in your local browser and enter the password you set at startup. `localhost` refers to your local device, and `7777` is the service port.

The iCode interface should appear after login, using any existing model configuration on the server. Once a model is configured, send “Hello” and check that you receive a reply.

The directory where you started the service is the initial working directory. Click the directory path at the right end of the conversation area's bottom border to [switch working directories](../daily-use/workspaces.md#change-the-working-directory-during-a-session). `icode serve` does not accept the TUI's `--workdir`, `--agent`, `--model`, or `--session` arguments. Switch agents or models and restore sessions in the page.

### 3. Finish using iCode

Keep the service terminal running while you use iCode. Once your tasks are finished, press `Ctrl+C` in that terminal to stop the service.

Each time you open a page and establish a connection, the server starts a separate iCode instance. Conversation history is saved on the server. After reopening the page, you can [restore a saved session](../daily-use/sessions.md#resume-an-existing-session).

Browser logins remain valid for 12 hours, and you must log in again after the service restarts. Five consecutive incorrect passwords from the same client address lock out that address. Only restarting the service clears the lockout. Restarting disconnects all connected pages.

## Access remote iCode from your local device

If you can already log in to the remote server over SSH, you can create an SSH tunnel to forward your local browser's connection to remote iCode over an encrypted channel, without configuring a domain. The following steps require two terminal windows on your local device.

### 1. Log in to the remote server and start iCode in the first terminal

```shell
ssh <user>@<server>
```

Replace `<user>` with the server's SSH username and `<server>` with its IP address or hostname. Log in using an SSH key or password.

After login, commands in this terminal run on the remote server. Change to the remote working directory, then start iCode:

```shell
icode serve --auth-required
```

Set a browser login password, confirm that the terminal displays the service URL, and keep the terminal running.

### 2. Create a tunnel in the second terminal

Open a second terminal on your local device and run:

```shell
ssh -N -o ExitOnForwardFailure=yes -L 7777:localhost:7777 <user>@<server>
```

Use the same username and server address as in the previous step. After SSH authentication, the terminal usually produces no further output and does not return to the command prompt. Keep it running.

If your SSH login requires a different port or a specific key, add the same connection options to both commands.

### 3. Open iCode in your local browser

Visit `http://localhost:7777` and enter the iCode browser login password you set in step 1.

When the browser connects to your local `localhost:7777`, the SSH tunnel forwards the connection to remote iCode. Keep `localhost` in the browser URL for this example; do not replace it with the remote IP address.

When your tasks are finished, press `Ctrl+C` in the first terminal to stop iCode and in the second terminal to close the SSH tunnel.

## Access iCode through a fixed HTTPS domain

To access iCode at a fixed address such as `https://icode.example.com`, deploy a reverse proxy. The browser establishes an encrypted connection to the proxy, which forwards requests to iCode.

This section assumes an existing HTTPS proxy. Configure the domain and certificate on the proxy; iCode itself does not serve HTTPS.

### 1. Start iCode

The following example requires the proxy and iCode to run on the same server. In a terminal on the server, change to your working directory, replace `icode.example.com` with your actual domain, and run:

```shell
icode serve --host 127.0.0.1 --port 7777 --public-url https://icode.example.com --auth-required
```

Set a browser login password and keep the terminal running. This command makes iCode accept connections only from the same server. The browser accesses the address specified by `--public-url` through the proxy.

### 2. Configure the proxy connection

The proxy must:

- Forward requests for the domain to `http://127.0.0.1:7777`.
- Preserve the original `Host` header from browser requests so iCode can check the access domain.
- Support WebSocket connections so the page can continuously transmit input and replies.

By default, iCode counts incorrect passwords by proxy address, so multiple visitors may share a failure count. If the proxy is trusted and correctly rewrites `X-Forwarded-For` (the client address request header), add `--auth-trust-forwarded-for` to the iCode startup command to count failures by the visitor address supplied by the proxy instead.

### 3. Verify access

Replace `icode.example.com` with your actual domain and open the corresponding HTTPS address in your local browser, such as `https://icode.example.com`. Do not add the iCode port `:7777`. The browser should display the iCode login page without a certificate warning. After login, send a message and confirm that you receive a reply.

The connection from the browser to the proxy is encrypted; the proxy still connects to iCode over HTTP within the server.

### Other deployment arrangements

If the proxy runs on another server or in a separate container, it may not be able to reach iCode at `127.0.0.1`. After adjusting the listening address, use firewall or container network rules to restrict access to the backend port to trusted proxies, preventing direct connections that bypass the HTTPS proxy. Setting an HTTPS `--public-url` does not encrypt the HTTP connection from the proxy to iCode.

## Read the password from a file or environment variable

When you cannot enter a password interactively in the terminal, provide it through a file or environment variable. Both methods enable authentication automatically, so `--auth-required` is unnecessary. You cannot use both methods at the same time.

### Use a password file

Create a UTF-8 plain text file on the server and write the login password to it. The password cannot be empty; trailing newlines in the file are ignored.

Change to your working directory and start iCode with the password file:

```shell
icode serve --auth-password-file "<password-file>"
```

Replace `<password-file>` with the path to the password file on the server. Startup no longer prompts for a password. Use the password in the file to log in through the browser.

### Use an environment variable

On the server, set an environment variable for the process that starts iCode, such as `CHRYS_SERVE_PASSWORD`, to a nonempty login password. Then run:

```shell
icode serve --auth-password-env CHRYS_SERVE_PASSWORD
```

The argument to `--auth-password-env` is the environment variable's name. Startup no longer prompts for a password. Use the variable's value to log in through the browser.

## Access iCode directly through a LAN IP address

Direct access through a LAN IP address transmits login passwords and conversation content in plain text. Use it only in a network environment where you explicitly accept this risk.

On the server, change to your working directory, replace `192.168.1.20` with the server's actual LAN IP address, and run:

```shell
icode serve --host 0.0.0.0 --public-url http://192.168.1.20:7777 --auth-required --allow-insecure-auth
```

`--host 0.0.0.0` allows other devices to connect to iCode. `--allow-insecure-auth` explicitly allows plain text password login over the network. Without this option, iCode refuses to start in this example.

In your local browser, visit the server's actual LAN IP address, such as `http://192.168.1.20:7777`, and log in. If you cannot connect, check that your local device can reach the server and that the server's firewall allows the client to access TCP port `7777`.

## Troubleshooting

### The `icode` command is not found

Confirm that iCode is installed on the server and that its installation directory is on `PATH`. If you just installed it, reopen the terminal and run this command to verify:

```shell
icode --version
```

### Local startup reports that the port is in use

Specify an available port with `--port` and use the same port in the browser URL. For example, if `8888` is available:

```shell
icode serve --port 8888 --auth-required
```

Then visit `http://localhost:8888`.

### The SSH tunnel reports that the local port is in use

Change the first port after `-L` to an available local port, keeping the last port set to the port remote iCode is listening on. For example, if remote iCode uses `7777` and local port `8888` is available, run this on your local device:

```shell
ssh -N -o ExitOnForwardFailure=yes -L 8888:localhost:7777 <user>@<server>
```

Replace `<user>` and `<server>` with the SSH username and server address. Also restart iCode on the remote server with the new browser access URL:

```shell
icode serve --public-url http://localhost:8888 --auth-required
```

Then visit `http://localhost:8888` on your local device.

### The browser cannot connect

First confirm that the iCode service is still running, then check that the browser URL matches your access method: use `localhost` for the local or SSH examples, the configured domain for HTTPS, or the server IP for a direct LAN connection.

If the address is correct but you still cannot connect, check that the SSH tunnel is running for SSH access, or check domain resolution and the proxy service for HTTPS access.

### The HTTPS page shows a certificate warning

Confirm that the browser is using the configured domain. Check whether the proxy certificate has expired, matches the domain, and was issued by an authority the browser trusts.

### The HTTPS page returns `502`

Check that iCode is still running, that the proxy's forwarding address and port match iCode's listening configuration, and that the proxy can reach that address. Consult the proxy logs for the specific cause.

### The page returns `421`

Use the domain or IP address and port displayed when iCode starts. When accessing through a proxy, check that it preserves the original `Host` header.

### The WebSocket connection returns `403`

Check that the browser URL matches the address specified by `--public-url`, including the scheme (`http` or `https`), domain, and port. iCode rejects connections with a mismatched origin.

### Login returns `403`

If the message is `Invalid login token`, reopen the login page and enter the password again. If it is `Invalid login origin`, check that the browser URL matches the access URL configured for iCode.

### Too many password attempts

The client address is locked out. First confirm that no tasks are running in other pages, then press `Ctrl+C` in the iCode terminal and restart the service. If multiple visitors using a proxy are locked out at once, check the conditions in the HTTPS section to determine whether enabling `--auth-trust-forwarded-for` is appropriate.

### The previous conversation is missing after a refresh or disconnection

When the page reconnects, the server starts a new iCode instance. Follow the steps for restoring a saved session to restore conversation history, but interrupted tasks do not resume automatically.

### Copying fails

If a copy dialog appears, click **Copy**. If copying still fails, select the text in the dialog and copy it manually using your browser's copy shortcut.
