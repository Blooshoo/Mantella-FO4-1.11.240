scriptName F4SE_HTTP native hidden

function sendLocalhostHttpRequest(int typedDictionaryHandle, int port, string route, int timeout = 0) global native

event OnHttpReplyReceived(int typedDictionaryHandle) native
event OnHttpErrorReceived(int typedDictionaryHandle) native

; Dictionary

Int function createDictionary() global native
function clearAllDictionaries() global native

String function getString(Int object, String key, String default="") global native
Int function getInt(Int object, String key, Int default=0) global native
Float function getFloat(Int object, String key, Float default=0.0) global native
Bool function getBool(Int object, String key, Bool default=false) global native
Int function getNestedDictionary(Int object, String key, Int default=0) global native
Int[] function getIntArray(Int object, String key) global native
Float[] function getFloatArray(Int object, String key) global native
String[] function getStringArray(Int object, String key) global native
Bool[] function getBoolArray(Int object, String key) global native
Int[] function getNestedDictionariesArray(Int object, String key) global native

bool function setString(Int object, String key, String value) global native
function setInt(Int object, String key, Int value) global native
function setFloat(Int object, String key, Float value) global native
function setBool(Int object, String key, Bool value) global native
function setNestedDictionary(Int object, String key, Int value) global native
function setIntArray(Int object, String key, Int[] value) global native
function setFloatArray(Int object, String key, Float[] value) global native
bool function setStringArray(Int object, String key, String[] value) global native
function setBoolArray(Int object, String key, Bool[] value) global native
function setNestedDictionariesArray(Int object, String key, Int[] value) global native

Bool function hasKey(Int object, String key) global native

Int function valueType(Int object, String key) global native