-- Phone Controllers: turns and tilts the view as the desktop app says, whichever camera is on.
-- Built like the game's own head tracking filter (trackir.lua): it runs after the camera has
-- worked out its view each frame, so there's no turning speed or range limit in the way.

local C = {}
C.__index = C

local socket = require('socket.socket')

local PORT = 4445             -- only on this PC: see BEAMNG_LOOK_ADDRESS in the desktop app
local STALE_SECONDS = 1       -- the app repeats itself, so silence means it stopped
local RETRY_SECONDS = 2
local MAX_ANGLE = 1440
local MAX_TILT = 89           -- any further and up turns into down

-- One socket and one angle however many camera contexts the game creates.
local udp = nil
local nextBind = 0
local angle = 0
local tilt = 0
local lastHeard = 0

local function listen(now)
  if udp or now < nextBind then return end
  nextBind = now + RETRY_SECONDS
  local candidate = socket.udp()
  candidate:settimeout(0)
  -- Fails for a moment after a Lua reload, until the old socket is collected.
  if candidate:setsockname('127.0.0.1', PORT) then
    udp = candidate
  else
    candidate:close()
  end
end

local function receive(now)
  listen(now)
  if not udp then return end
  while true do
    local message = udp:receive()
    if not message then break end
    local turnText, tiltText = message:match("^(%S+) (%S+)$")
    local turnValue, tiltValue = tonumber(turnText), tonumber(tiltText)
    if turnValue and tiltValue and turnValue == turnValue and tiltValue == tiltValue then
      angle = clamp(turnValue, -MAX_ANGLE, MAX_ANGLE)
      tilt = clamp(tiltValue, -MAX_TILT, MAX_TILT)
      lastHeard = now
    end
  end
  if now - lastHeard > STALE_SECONDS then angle, tilt = 0, 0 end
end

function C:init()
  self.isGlobal = true
  self.runningOrder = 0.55 -- after the head tracking filter, before the fallback camera
  self.isFilter = true
  self.hidden = true
end

function C:update(data)
  if data.openxrSessionRunning then return end
  receive(socket.gettime())
  if angle ~= 0 or tilt ~= 0 then
    -- Around the world's vertical, like a head turning: around the camera's own up axis, a
    -- camera that looks down would tilt the horizon. Positive is to the right.
    -- The tilt goes last, around the camera's own right, so the horizon stays level whichever
    -- way it now faces. Positive is up.
    local turned = data.res.rot * quatFromEuler(0, 0, math.rad(angle))
    data.res.rot = quatFromEuler(-math.rad(tilt), 0, 0) * turned
  end
  return true
end

return function(...)
  local o = ... or {}
  setmetatable(o, C)
  o:init()
  return o
end
