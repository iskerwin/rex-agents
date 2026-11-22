-- rex-agents: a pane down the right edge of a Rex tab with how much quota
-- Claude Code, Codex and Grok have left, the advisor of this tab's Claude Code
-- sessions, today's and this month's cost, and the agents running. It runs
-- `rex-agents`, so it sits beside any agent, or none.
--
-- Load it from Rex's config (~/.config/rex/init.lua):
--
--   dofile("/path/to/rex-agents/rex/init.lua").setup()
--
-- `setup` takes `bin`, the command as a list (default `{ "rex-agents" }`),
-- `width`, the pane's share of the tab's width (default 1/3; 1/4 for less),
-- and `key`, the binding for the toggle (default shift+cmd+u; false for none).

local M = {}

local SHELL = "com.superlogical.terminal.shell"
local LABEL = "Agents"

-- The tiled layer of the window that holds `block_id`, and that window.
local function placement(view, block_id)
  for _, window in ipairs(view.windows or {}) do
    for _, layer in ipairs(window.layers or {}) do
      if layer.kind == "tiled" then
        for _, block in ipairs(layer.blocks or {}) do
          if block.block_id == block_id then return window, layer end
        end
      end
    end
  end
end

-- The block or split at the root of a layout: splitting it puts the pane
-- beside everything in the tab, the full height of the window.
local function root_id(node)
  if node.block_id ~= nil then return node.block_id end
  return node.split and node.split.split_id
end

function M.setup(opts)
  opts = opts or {}
  local bin = opts.bin or { "rex-agents" }
  local width = opts.width or 1 / 3
  if type(width) ~= "number" or width <= 0 or width >= 1 then
    error("rex-agents: width must be a share of the tab between 0 and 1, such as 1/3", 0)
  end
  if type(bin) == "string" then bin = { bin } end
  local command = {}
  for _, part in ipairs(bin) do command[#command + 1] = part end

  rex.action{
    name = "agents_panel",
    title = "Toggle Agents Panel",
    description = "Show or hide quota, advisor, cost and running agents down the right edge of this tab",
    category = "rex-agents",
    keywords = "quota usage advisor claude codex grok 额度 用量",
    run = function(ctx)
      -- The usage is this machine's, read from its logs and logins.
      if ctx.server ~= nil then error("rex-agents: only for sessions on this machine", 0) end
      if ctx.block_id == nil then error("rex-agents: no pane to open beside", 0) end
      local view, err = rex.session.view{}
      if view == nil then error("rex-agents: " .. tostring(err), 0) end
      local _, layer = placement(view, ctx.block_id)
      if layer == nil then error("rex-agents: open it from a tiled pane", 0) end

      for _, block in ipairs(layer.blocks) do
        if block.label == LABEL and block.flavor == SHELL then
          local _, close_err = rex.call("block.close", { session_id = ctx.session_id, block_id = block.block_id })
          if close_err ~= nil then error("rex-agents: " .. tostring(close_err), 0) end
          return
        end
      end

      -- The share the tab keeps: what is there goes before the pane.
      local ratio = 1 - width
      local split, split_err = rex.session.new_split{
        anchor_id = root_id(layer.layout),
        direction = "horizontal",
        side = "after",
        ratio = ratio,
        focus = false,
        layout = { block = { flavor = SHELL, label = LABEL, options = { command = command } } },
      }
      if split_err ~= nil then error("rex-agents: couldn't open the pane: " .. tostring(split_err), 0) end
      -- Rex 0.1.0 splits a new pane in half once the app attaches to it,
      -- whatever `ratio` said, so the border is set again after that.
      local pane = split and split.block_ids and split.block_ids[1]
      if pane ~= nil then
        rex.exec({ "sleep", "0.4" })
        rex.session.resize_pane{
          direction = "horizontal",
          before_block_id = ctx.block_id,
          after_block_id = pane,
          ratio = ratio,
        }
      end
    end,
  }

  if opts.key ~= false then rex.bind(opts.key or "shift+cmd+u", "agents_panel") end
end

return M
