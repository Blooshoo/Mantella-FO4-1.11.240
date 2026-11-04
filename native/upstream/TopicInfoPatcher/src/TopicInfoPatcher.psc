Scriptname TopicInfoPatcher extends ScriptObject Native

int Function PatchTopicInfo (Topic tpTopic, string text) Native Global

Function ClearCache() Native Global

Function SetOverrideFileName(Topic TPInfo, string name) Native Global

string Function StringRemoveWhiteSpace(string text) native Global

; filetype = 3 PNG 
; filetype = 2 TGA

Function TakeScreenShot(string filename, int filetype, int sstype = 1) native global
bool Function isMenuModeActive() native global

; Functions to save and restore GameSettings

bool Function saveFloat(string key) native global
bool Function saveInt(string key) native global
bool Function restoreFloat(string key) native global
bool Function restoreInt(string key) native global

; returning Actor values from plugins is problematic at this time
; Either some Actors return as none, or will cause the game to CTD.
; As a workaround we just return the coords for the crosshair actor
; And then we just use Game.FindClosestActor() to get the Actor

Float [] Function GetLastActorCoords() native global
