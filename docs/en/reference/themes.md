# Theme File Reference

iCode supports theme files for customizing the appearance of the terminal user interface (TUI). This page describes the file format, common color fields, and loading rules. To edit and save themes through the interface, see [Customize themes](../guides/configuration/themes.md).

## File Location and Naming

Store theme files in the following directory. Create it first if it does not exist.

| Platform | Directory |
| --- | --- |
| macOS / Linux | `~/.chrys/themes/` |
| Windows | `%APPDATA%\chrys\themes\` |

Theme files must have a `.yaml` or `.yml` extension. UTF-8 is recommended; iCode also detects common legacy encodings such as GBK/GB18030 and BOM-marked UTF-16/UTF-32. Themes saved through iCode use UTF-8. Place files directly in this directory; iCode does not read theme files in subdirectories.

Files that cannot be decoded using the detected encoding are skipped with a warning, just like invalid YAML files.

The filename with the `.yaml` or `.yml` extension removed becomes the theme name. Theme names must meet these requirements:

* Start with an English letter or digit and contain only English letters, digits, dots, underscores, and hyphens.
* Not match a built-in theme name, ignoring case. Built-in names include `chrys`, `chrys-legacy`, `chrys-ansi`, the Textual themes in the theme list (such as `nord` and `textual-dark`), and the retired name `chrys-dark`.
* Not end in `.yaml` or `.yml`, ignoring case. For example, `my-theme.yaml.yaml` is not a valid filename.

When a `.yaml` file and a `.yml` file have the same theme name, iCode prefers `.yaml`. If the `.yaml` file is invalid, iCode still tries the `.yml` file. Only one theme is kept per name.

After adding or editing a theme file, restart iCode to load it, then press **F9** and select the corresponding theme from the theme list to apply it.

## File Format

The following example defines a custom dark theme in `icode-ocean.yaml`:

```yaml
# Overall palette
primary: "#5c9ded"
secondary: "#7aa2c7"
accent: "#70c1b3"
warning: "#e5b567"
error: "#e57373"
success: "#81c784"
dark: true
background: "#18212b"
foreground: "#e6edf3"
surface: "#202c38"
panel: "#263442"

# Colors for specific areas
variables:
  footer-background: "#18212b"
  footer-foreground: "#e6edf3"
  input-selection-background: "#5c9ded 35%"
  markdown-block-background: "#202c38"
```

`primary` is the only required field. All other fields are optional. iCode fills in omitted colors automatically; see [Default Color Rules](#default-color-rules).

## Configuration Fields

A theme file has two parts:

* **Top-level fields** set the overall palette.
* **`variables`** sets colors and styles for specific interface areas.

`variables` must be a mapping from strings to strings. Unknown top-level fields cause the theme to be skipped.

### Overall Palette

The following fields set the theme's base colors.

| Purpose | Field | Description |
| --- | --- | --- |
| Primary color | `primary` | The theme's main identifying color, used for headings and emphasized backgrounds |
| Secondary emphasis color | `secondary` | Used for elements that should stand apart from the primary color |
| Accent color | `accent` | Used sparingly to draw attention, usually contrasting with `primary` and `secondary` |
| Status colors | `warning`, `error`, `success` | Background, border, and text colors for warning, error, and success states, respectively |
| Overall background | `background` | The default background for areas not covered by widgets |
| Default text color | `foreground` | Should remain readable against `background`, `surface`, and `panel` |
| Widget background | `surface` | The default widget background, typically used for widgets above the overall background |
| Panel color | `panel` | Visually separates an interface area from the main content |
| Dark or light mode | `dark` | `true` for dark mode, `false` for light mode; affects omitted background colors and automatically generated colors |
| ANSI color mode | `ansi` | Defaults to `false`. Set to `true` to enable terminal ANSI color mode, which supports values such as `ansi_red` and `ansi_default`. The terminal palette determines the actual ANSI colors. This mode ignores the top-level `surface` and `panel` settings and uses transparent colors for both |

Color fields must be strings. Quote hexadecimal values, such as `"#5c9ded"`. Other color formats, such as `"rgb(92, 157, 237)"` or `"red"`, are also supported.

Top-level color fields can use `"rgba(92, 157, 237, 0.35)"` for 35% opacity. Percentage suffixes such as `"#5c9ded 35%"` are not supported in these fields.

Do not leave fields empty or set them to `null`. Omit fields you do not need to configure.

### Buttons, Footer, Selection, and Cursor

Place the following fields under `variables`. Each affects only the corresponding interface element. Color values under `variables` support percentage suffixes. For example, `input-selection-background: "#5c9ded 35%"` blends the color at 35% opacity. Percentage blending applies to RGB colors; `ansi_*` colors ignore opacity.

| Purpose | Field |
| --- | --- |
| Button text | `button-flat-foreground` |
| Button text and background on hover | `button-hover-foreground`, `button-hover-background` |
| Disabled button text and background | `button-disabled-foreground`, `button-disabled-background` |
| Footer background and text | `footer-background`, `footer-foreground` |
| Footer shortcut key background and text | `footer-key-background`, `footer-key-foreground` |
| Input selection background | `input-selection-background` |
| Block cursor background and text | `block-cursor-background`, `block-cursor-foreground` |
| Block cursor text style | `block-cursor-text-style` |

Omitted button colors use the theme's default palette.

`block-cursor-text-style` accepts a Textual text style such as `"bold"` (the default), `"italic"`, `"underline"`, `"reverse"`, or `"none"` (no text styling).

### Borders, Widgets, and Content Areas

Place the following fields under `variables` as well. Omitted fields use the default colors listed below.

| Field | Color when omitted | Affected interface elements |
| --- | --- | --- |
| `border-color` | Each area's existing colors | Unifies ordinary interface borders; message, warning, and error borders retain their respective colors, and button borders still follow button backgrounds |
| `tool-group-title-color` | `warning` | Tool call group titles |
| `control-background`, `control-background-muted` | `surface` | Widget backgrounds and muted backgrounds |
| `control-disabled-background` | `background` | Disabled widget backgrounds |
| `control-foreground-muted` | `text-disabled` | Muted widget text |
| `overlay-background` | `panel` | Tooltip and notification backgrounds |
| `markdown-block-background` | `surface` | Markdown blockquote and fenced code block backgrounds |
| `hatch-color` | `foreground` blended at 15% opacity | Diagonal hatching in empty-state areas |

Use `scrollbar`, `scrollbar-hover`, and `scrollbar-active` to configure scrollbar colors in normal, hover, and drag states. `scrollbar-background` sets the track background for all three states; existing `scrollbar-background-hover` and `scrollbar-background-active` values can override the corresponding states individually.

## Default Color Rules

For non-ANSI themes, omitted fields use the following defaults or are calculated from other colors. ANSI mode uses terminal colors, so do not use this table to infer its final displayed colors.

| Omitted field | Value used |
| --- | --- |
| `dark` | `true` (dark mode) |
| `secondary`, `accent`, `warning` | Same as `primary` |
| `error`, `success` | Same as `secondary` |
| `background` | `#121212` in dark mode; `#efefef` in light mode |
| `foreground` | The inverse of the background color |
| `surface` | `#1e1e1e` in dark mode; `#f5f5f5` in light mode |
| `panel` | A blend based on `surface`, `primary`, and dark or light mode |

The following optional top-level fields adjust the automatically generated palette.

| Field | Default behavior | What it adjusts |
| --- | --- | --- |
| `luminosity_spread` | Defaults to `0.15` | Controls how much generated light and dark shades differ from the base colors; must be a finite number, not `.nan` or `.inf` |
| `boost` | In dark mode with `panel` omitted, overlays black or white at 4% opacity on the generated panel to contrast with the background | Accepts a color string. In dark mode with `panel` omitted, it replaces that overlay, adjusting the generated panel's brightness, and also sets the default `block-hover-background` (this color at 10% opacity). No effect in light mode or when `panel` is specified |

The top-level field `text_alpha` is also accepted and must be a finite number, but it currently does not change displayed colors.

## Loading Failures

If iCode finds malformed YAML, a missing `primary` field, unknown top-level fields, or invalid field values while loading, it displays a warning and skips the file. Use the warning to check the corresponding file, correct it, and restart iCode to load it again. Other valid themes remain available.

If a theme loads successfully but a `variables` value cannot be used in the corresponding interface style when applied, iCode displays a warning and falls back to the default `chrys` theme. This fallback does not overwrite the saved theme selection. Correct the file, then restart iCode to try again.
