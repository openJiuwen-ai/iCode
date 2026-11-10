# Customize themes

Use the theme editor to adjust colors in the terminal user interface (TUI) and preview the results directly. This guide covers editing an existing theme, saving your own theme, undoing changes, and deleting themes.

Color changes are previewed live but are not saved automatically. Save your changes through the editor's “Save” dialog to write them to a theme file and keep them for the next launch. To write YAML files directly or look up color fields, see the [Theme file reference](../../reference/themes.md).

## Open the editor

1. Press **F9**, or click `f9 Themes` in the footer, to open the theme list.
2. Select “Edit themes…” at the bottom of the list to open the theme editor beside the main interface.
3. Use the dropdown at the top of the editor to choose a theme to edit. This changes the editing target without changing the applied theme selection.

The current conversation remains visible beside the editor, so you can see how colors affect text, tool cards, and other interface elements.

While the editor is open, switching the applied theme through F9, `/theme`, or the settings window is restricted. Use the editor's dropdown to choose another theme to edit. To change the theme for everyday use, close the editor first.

## Adjust and preview colors

The editor groups colors under headings such as `Colors`, `Buttons`, and `Borders`. Each color is identified by its field name, such as `primary` (the main color) or `background` (the overall background).

1. Click a color swatch to open the color picker.
2. For non-ANSI themes, choose or enter a color under `RGB / HSV`, or switch to “256 colors” to select a color. ANSI themes offer `ANSI` and “256 colors” modes. The actual appearance of ANSI colors depends on your terminal palette.
3. The main interface previews your adjustments live. Click “Confirm” to keep the change in the editor. Click “Cancel” or press **Esc** to discard changes made during this visit to the color picker.

Background and panel colors (`background`, `surface`, `panel`, and `boost`) must remain opaque, so the color picker does not offer transparency controls for these fields.

## Undo and reset

Choose an action based on how much you want to undo:

| Action | Result |
| --- | --- |
| “Restore” in the color picker | Restores the value from when you opened the picker |
| “Undo” / “Redo” in the editor | Undoes or redoes confirmed edits |
| The “↺” reset button beside a changed field | Restores that field to its value when the theme was loaded into the editor |
| “Reset all” | Restores all fields to their values when the theme was loaded into the editor |

## Save a theme

1. Click “Save” in the editor.
2. Enter a theme name in the “Save theme” dialog.
3. Click “Save”. Once the success notification appears, the theme is applied immediately and the selection is remembered.

Theme names must follow these rules:

* Use no more than 100 characters. Start with an ASCII letter or digit, and use only ASCII letters, digits, dots, underscores, and hyphens. Do not end the name with a dot.
* Do not include a `.yaml` or `.yml` extension.
* When editing a built-in theme, save under a new name. When editing a user theme, an exact match with the original name (including letter case) overwrites the original file; a new name saves a separate file. Duplicate-name checks are case-insensitive.
* Do not use Windows device names such as `CON`, `NUL`, `COM1`–`COM9`, or `LPT1`–`LPT9`. Names formed by adding a dot and suffix to these names, such as `CON.dark`, are also prohibited.

## Close the editor or switch editing targets

If there are unsaved changes when you select another theme to edit or click “Close”, you will be asked whether to discard them. Choose “Discard” to discard the changes and continue, or “Cancel” to keep editing.

When the editor closes, the interface returns to the currently applied theme.

## Delete a user theme

“Delete” removes the theme file and any unsaved changes to that theme. Built-in themes cannot be deleted.

Select a user theme at the top of the editor and click “Delete”. Check the theme name in the confirmation dialog, then click “Delete” again. Deleting the currently applied theme switches iCode to the default theme, `chrys`.
